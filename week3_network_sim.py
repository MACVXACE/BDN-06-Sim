"""
week3_network_sim.py
=====================
MAC Layer & Network Logic (Week 3) for the intrabody/body-coupled
discrete-event link simulator.

Builds on the validated Week 2 physical layer (channel_model.py) to model
an *active wideband interference-masking* scheme intended to protect the
primary galvanic (differential) link A->B from the capacitive cross-
coupling eavesdropping path to node C.

Topology
--------
    Node A (TX)  --galvanic, differential-------->  Node B (primary RX)
        |         gain_ab_db  = -40.27 dB @ 500 kHz  (locked, Week 2)
        |
        +--capacitive fringing (single-ended)----->  Node C (eavesdropper)
                  gain_leak_db = -15.15 dB @ 500 kHz  (locked, Week 2)

Node A also injects a PRBS-23 wideband masking carrier onto the
capacitive cross-coupling path (same physical mechanism that produces the
A->C leak). Node B's differential front end is assumed to reject this
masking energy by its CMRR; Node C, being single-ended, gets none of that
rejection.

MODELING ASSUMPTIONS NOT SPECIFIED IN THE BRIEF (all centralized in
`SimConfig` below -- change these to match your real front-end):

  * The masking carrier reaches Node B's *common-mode* node via the same
    -15.15 dB capacitive path that produces the A->C leak. This is the
    only "capacitive cross-coupling" figure we have, and re-using it for
    both destinations is the most physically consistent reading of "Node A
    ... broadcasts ... over the capacitive cross-coupling path" -- but it
    is an assumption, not a given. See the critique for why this matters.
  * Modulation: BPSK, used only to turn SINR into a bit-error probability
    via the Gaussian Q-function -- swap `ber_bpsk()` for your real
    modem's BER(SINR) curve.
  * Packet size: 1000 bits/packet.
  * Receiver bandwidth 200 kHz, front-end noise figure 20 dB -> thermal
    noise floor computed from kTB, not hand-picked.
  * Dry-contact channel instability: each packet's link and leakage gains
    are jittered by a Gaussian (in dB) to stand in for dynamic electrode
    contact-impedance variation, since "Dry Contact" was specified
    qualitatively but not with a variance.
  * CMRR degradation: modeled two ways and combined by taking the worse
    (see `effective_system_cmrr_db`) -- frequency rolloff of the
    amplifier's own CMRR, and common-mode-to-differential-mode conversion
    from source (electrode) impedance mismatch. Both use generic,
    textbook-order-of-magnitude constants, not a specific datasheet.

Run directly: `python week3_network_sim.py`
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field

import simpy

from channel_model import sphere_sphere_fringing_leakage_db  # noqa: F401  (kept for future re-parameterization)

# ---------------------------------------------------------------------------
# Locked Week 2 physical-layer operating point (500 kHz, 10 cm profile)
# ---------------------------------------------------------------------------
FREQ_HZ = 500e3
GAIN_AB_DB = -40.27     # primary differential link A->B (galvanic_symmetric_gain_db output)
GAIN_LEAK_DB = -15.15   # capacitive cross-coupling leak A->C (sphere_sphere_fringing_leakage_db output)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass
class SimConfig:
    # RF / link budget
    freq_hz: float = FREQ_HZ
    tx_primary_dbm: float = -10.0
    tx_mask_dbm: float = -17.0
    gain_ab_db: float = GAIN_AB_DB
    gain_leak_db: float = GAIN_LEAK_DB

    # Node B differential front end
    cmrr_dc_db: float = 86.0            # spec'd / assumed low-frequency CMRR
    cmrr_corner_hz: float = 10e3        # assumed knee where CMRR starts rolling off (generic in-amp behavior)
    cmrr_rolloff_db_per_decade: float = 20.0
    z_in_ohm: float = 10e6              # assumed differential amplifier input impedance
    delta_z_ohm: float = 50e3           # assumed dry-contact electrode impedance MISMATCH (worst case)
    use_realistic_cmrr: bool = True     # False = ideal 86 dB flat, ignoring rolloff/mismatch

    # Receiver noise
    rx_bandwidth_hz: float = 200e3
    noise_figure_db: float = 20.0
    temp_k: float = 290.0

    # Channel dynamics (dry-contact instability)
    contact_jitter_std_db: float = 3.0       # per-packet gain jitter, both links
    coupling_asymmetry_std_db: float = 4.0   # extra jitter between "mask->B common mode" and "mask->C" paths

    # MAC / traffic
    packet_bits: int = 1000
    bitrate_bps: float = 100e3
    interpacket_gap_s: float = 0.005
    n_packets: int = 3000
    seed: int | None = 1234


# ---------------------------------------------------------------------------
# RF helpers
# ---------------------------------------------------------------------------
def dbm_to_mw(dbm: float) -> float:
    return 10 ** (dbm / 10.0)


def mw_to_dbm(mw: float) -> float:
    return 10.0 * math.log10(mw)


def sinr_db(signal_dbm: float, interference_dbm: float, noise_dbm: float) -> float:
    """SINR in dB from three power levels in dBm (combined linearly in mW)."""
    sig = dbm_to_mw(signal_dbm)
    intf = dbm_to_mw(interference_dbm)
    noise = dbm_to_mw(noise_dbm)
    return mw_to_dbm(sig / (intf + noise))


def thermal_noise_floor_dbm(bandwidth_hz: float, temp_k: float, noise_figure_db: float) -> float:
    """kTB noise floor + receiver noise figure, in dBm."""
    k = 1.380649e-23
    noise_w = k * temp_k * bandwidth_hz
    return mw_to_dbm(noise_w * 1000.0) + noise_figure_db


def freq_derated_cmrr_db(cmrr_dc_db: float, corner_hz: float, f_hz: float, rolloff_db_per_decade: float) -> float:
    """CMRR above its corner frequency rolls off like a single-pole response."""
    if f_hz <= corner_hz:
        return cmrr_dc_db
    decades = math.log10(f_hz / corner_hz)
    return cmrr_dc_db - rolloff_db_per_decade * decades


def impedance_limited_cmrr_db(z_in_ohm: float, delta_z_ohm: float) -> float:
    """
    First-order common-mode-to-differential-mode conversion limit from
    source (electrode) impedance mismatch: V_diff/V_cm ~= delta_Z / Z_in
    for Z_in >> source impedances. This is a system-level ceiling on CMRR
    that the amplifier's own datasheet CMRR does NOT capture.
    """
    if delta_z_ohm <= 0:
        return float("inf")
    return 20.0 * math.log10(z_in_ohm / delta_z_ohm)


def effective_system_cmrr_db(cfg: SimConfig) -> float:
    if not cfg.use_realistic_cmrr:
        return cfg.cmrr_dc_db
    freq_limited = freq_derated_cmrr_db(
        cfg.cmrr_dc_db, cfg.cmrr_corner_hz, cfg.freq_hz, cfg.cmrr_rolloff_db_per_decade
    )
    z_limited = impedance_limited_cmrr_db(cfg.z_in_ohm, cfg.delta_z_ohm)
    return min(freq_limited, z_limited)  # the worse mechanism dominates


# ---------------------------------------------------------------------------
# Link-layer error model
# ---------------------------------------------------------------------------
def q_function(x: float) -> float:
    return 0.5 * math.erfc(x / math.sqrt(2))


def ber_bpsk(sinr_linear: float) -> float:
    if sinr_linear <= 0:
        return 0.5
    return q_function(math.sqrt(2.0 * sinr_linear))


def packet_success_prob(sinr_dB: float, packet_bits: int) -> float:
    sinr_lin = 10 ** (sinr_dB / 10.0)
    ber = ber_bpsk(sinr_lin)
    per = 1.0 - (1.0 - ber) ** packet_bits
    return 1.0 - per


# ---------------------------------------------------------------------------
# Discrete-event packet source / channel / receivers
# ---------------------------------------------------------------------------
@dataclass
class NodeStats:
    sinr_db_samples: list = field(default_factory=list)
    delivered: int = 0
    total: int = 0

    def record(self, sinr: float, success: bool) -> None:
        self.sinr_db_samples.append(sinr)
        self.total += 1
        if success:
            self.delivered += 1

    @property
    def pdr(self) -> float:
        return self.delivered / self.total if self.total else float("nan")

    @property
    def mean_sinr_db(self) -> float:
        return statistics.mean(self.sinr_db_samples) if self.sinr_db_samples else float("nan")


def transmitter_process(env: simpy.Environment, cfg: SimConfig, stats_b: NodeStats, stats_c: NodeStats,
                         noise_floor_dbm: float, system_cmrr_db: float):
    """
    Node A: emits `cfg.n_packets` MAC frames back-to-back (packet airtime +
    interpacket gap), each accompanied by the continuous PRBS-23 masking
    carrier. Per packet, both links' gains are jittered to represent
    dry-contact instability, and the masking carrier's coupling to B's
    common-mode node vs. to C is allowed to differ slightly (they are not
    literally the same measurement point).
    """
    packet_airtime_s = cfg.packet_bits / cfg.bitrate_bps

    for seq in range(cfg.n_packets):
        gain_ab = cfg.gain_ab_db + random.gauss(0.0, cfg.contact_jitter_std_db)
        gain_leak_c = cfg.gain_leak_db + random.gauss(0.0, cfg.contact_jitter_std_db)
        gain_leak_b_mask = (
            cfg.gain_leak_db
            + random.gauss(0.0, cfg.contact_jitter_std_db)
            + random.gauss(0.0, cfg.coupling_asymmetry_std_db)
        )

        # --- Node B: differential primary signal, common-mode-rejected mask ---
        sig_at_b_dbm = cfg.tx_primary_dbm + gain_ab
        mask_common_at_b_dbm = cfg.tx_mask_dbm + gain_leak_b_mask
        mask_diff_at_b_dbm = mask_common_at_b_dbm - system_cmrr_db
        sinr_b = sinr_db(sig_at_b_dbm, mask_diff_at_b_dbm, noise_floor_dbm)

        # --- Node C: single-ended, no differential rejection at all ---
        sig_at_c_dbm = cfg.tx_primary_dbm + gain_leak_c
        mask_at_c_dbm = cfg.tx_mask_dbm + gain_leak_c
        sinr_c = sinr_db(sig_at_c_dbm, mask_at_c_dbm, noise_floor_dbm)

        success_b = random.random() < packet_success_prob(sinr_b, cfg.packet_bits)
        success_c = random.random() < packet_success_prob(sinr_c, cfg.packet_bits)

        stats_b.record(sinr_b, success_b)
        stats_c.record(sinr_c, success_c)

        yield env.timeout(packet_airtime_s + cfg.interpacket_gap_s)


def run_scenario(name: str, cfg: SimConfig) -> dict:
    if cfg.seed is not None:
        random.seed(cfg.seed)

    noise_floor_dbm = thermal_noise_floor_dbm(cfg.rx_bandwidth_hz, cfg.temp_k, cfg.noise_figure_db)
    system_cmrr_db = effective_system_cmrr_db(cfg)

    env = simpy.Environment()
    stats_b, stats_c = NodeStats(), NodeStats()
    env.process(transmitter_process(env, cfg, stats_b, stats_c, noise_floor_dbm, system_cmrr_db))
    env.run()

    return {
        "name": name,
        "noise_floor_dbm": noise_floor_dbm,
        "system_cmrr_db": system_cmrr_db,
        "pdr_b": stats_b.pdr,
        "pdr_c": stats_c.pdr,
        "mean_sinr_b_db": stats_b.mean_sinr_db,
        "mean_sinr_c_db": stats_c.mean_sinr_db,
        "sim_time_s": env.now,
    }


def print_report(result: dict) -> None:
    print(f"--- {result['name']} ---")
    print(f"  effective system CMRR at Node B : {result['system_cmrr_db']:6.2f} dB")
    print(f"  assumed rx noise floor            : {result['noise_floor_dbm']:6.2f} dBm")
    print(f"  Node B  mean SINR / PDR            : {result['mean_sinr_b_db']:6.2f} dB  /  {result['pdr_b']*100:6.2f} %")
    print(f"  Node C  mean SINR / PDR            : {result['mean_sinr_c_db']:6.2f} dB  /  {result['pdr_c']*100:6.2f} %")
    print(f"  simulated {result['sim_time_s']*1000:.1f} ms of traffic")
    print()


def main() -> None:
    ideal_cfg = SimConfig(use_realistic_cmrr=False)
    realistic_cfg = SimConfig(use_realistic_cmrr=True)

    ideal = run_scenario("Scenario A: datasheet-ideal CMRR (86 dB flat, perfectly symmetric coupling)", ideal_cfg)
    realistic = run_scenario("Scenario B: frequency- and mismatch-derated CMRR at 500 kHz", realistic_cfg)

    print_report(ideal)
    print_report(realistic)

    print("=== Headline numbers ===")
    print(f"TX power gap (primary - mask)        : {ideal_cfg.tx_primary_dbm - ideal_cfg.tx_mask_dbm:.2f} dB")
    print(f"Node C SINR is ~ this TX power gap regardless of CMRR, because the")
    print(f"signal and the masking carrier share the SAME leakage path to C.")


if __name__ == "__main__":
    main()
