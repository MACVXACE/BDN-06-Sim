# BDN-06: Intrabody Link Simulator (IBLS)

### A discrete-event physical- and MAC-layer simulator for galvanic & capacitive body-coupled communication links

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![SimPy](https://img.shields.io/badge/simpy-4.x-informational)
![Status](https://img.shields.io/badge/status-research%20%2F%20experimental-orange)
![License](https://img.shields.io/badge/license-MIT-lightgrey)


---

## Executive Summary

This repository is a two-layer, discrete-event simulation framework for **body-coupled communication (BCC)** links — the class of near-field channels used by wearable and on-body sensor networks, where the signal path is the human body itself rather than free-space RF propagation.

The framework is built in two stages:

- **Week 2 — Physical Layer** (`channel_model.py`): closed-form, parameterized physics models for the two coupling mechanisms that coexist in any body-coupled link — **galvanic (conductive, differential)** and **capacitive (displacement-current, single-ended)** — including tissue impedance, electrode fringing capacitance, and near-field cross-coupling between a legitimate receiver and a nearby unintended one.
- **Week 3 — MAC Layer & Network Logic** (`week3_network_sim.py`): a `simpy`-driven packet-level simulation that consumes the Week 2 channel outputs, models a link-layer error process (BER → PER → packet delivery), and evaluates an **active interference-masking security scheme** meant to protect the primary link against a capacitively-coupled eavesdropper.

The headline result of the Week 3 work is a validated, quantitative counter-example to a common physical-layer-security assumption: **a high common-mode rejection ratio (CMRR) at the legitimate receiver does not, by itself, deny an eavesdropper anything.** The simulator derives, and then confirms by Monte Carlo, that eavesdropper SINR in this topology is governed entirely by the transmit-power gap between the primary signal and the masking signal — not by CMRR, and not by channel attenuation. See [Security Architecture](#security-architecture) below.

---

## Repository Structure

```
.
├── channel_model.py        # Week 2 — physical layer: tissue/electrode/coupling models
├── week3_network_sim.py    # Week 3 — MAC/network layer: simpy packet simulation + security analysis
├── requirements.txt
└── README.md
```

`week3_network_sim.py` imports directly from `channel_model.py`, so the two files must live in the same directory (or `channel_model` must be importable on `PYTHONPATH`).

---

## Installation

Requires Python 3.10+ (uses `from __future__ import annotations` and PEP 604-style type hints internally).

```bash
git clone <your-repo-url>
cd <your-repo>
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

`requirements.txt`:
```
simpy>=4.0
numpy>=1.20
```

## Usage

Run the Week 3 simulation directly:

```bash
python3 week3_network_sim.py
```

This executes two Monte Carlo scenarios over a batch of 3,000 simulated packets each — an idealized-CMRR run and a frequency/impedance-derated run — and prints a per-scenario report (effective system CMRR, receiver noise floor, mean SINR, and Packet Delivery Ratio for both the primary receiver and the eavesdropper), followed by the headline TX-power-gap finding.

To reproduce the mitigated-state result (Scenario C below), import the config directly:

```python
from week3_network_sim import SimConfig, run_scenario, print_report

cfg = SimConfig(tx_mask_dbm=-10.0, use_realistic_cmrr=True)  # close the TX gap to 0 dB
print_report(run_scenario("Mitigated state", cfg))
```

All physical and MAC-layer parameters — TX powers, link gains, CMRR corner frequency, electrode impedance mismatch, receiver noise figure, packet size, bitrate, per-packet channel jitter — are centralized in the `SimConfig` dataclass at the top of `week3_network_sim.py`, so the whole scenario can be re-parameterized without touching the simulation logic.

---

## Physical Channel Models

The link topology models three nodes sharing a body-coupled medium: a transmitter (**Node A**), an intended differential receiver (**Node B**), and an unintended single-ended receiver (**Node C**) that is close enough to pick up cross-coupled energy from A. `channel_model.py` implements the two coupling mechanisms that determine how much energy reaches each of them.

### 1. Galvanic (conductive, differential) coupling — the primary link

`galvanic_symmetric_gain_db(...)` models the intended A→B link, where current is injected into tissue at one electrode pair and sensed differentially at another. The closed-form gain combines three terms:

- **Bulk resistive divider** between muscle and skin resistance (`muscle_resistance`, `skin_impedance_elements`), reflecting that muscle is a comparatively good conductor while the skin interface is the dominant series impedance.
- **Frequency-dependent skin-vs-muscle reactance ratio**, since the skin layer's RC time constant and the muscle's own reactive rolloff scale differently with frequency.
- **Geometric attenuation term**, a function of electrode spacing, link distance, electrode radius, and the transmit/receive electrode-pair orientation angle — this is what makes the link *differential*: it depends on the relative geometry of two electrode pairs, not a single point-to-point distance.

`skin_impedance_elements(sigma_skin, eps_r_skin, t_skin, r)` derives the per-electrode skin resistance and capacitance from tissue conductivity, permittivity, thickness, and contact radius — this is where **dry-contact vs. gelled-electrode** assumptions enter the model (the framework is currently parameterized for dry contact, which has materially higher and less stable impedance than gelled electrodes).

### 2. Capacitive (displacement-current, single-ended) cross-coupling — the leakage path

Two complementary models capture the unintended A→C path:

- `sphere_sphere_fringing_leakage_db(r, dcc, freq_hz, z_load)` treats the near-field coupling between two conductive bodies (e.g., Node A's transmitter enclosure and Node C's pickup) as a fringing capacitance between two spheres, derives the resulting coupling impedance at the operating frequency, and expresses the leakage as a ratio against the receiver's load impedance.
- `CapacitiveHBCLink` models the more general capacitive HBC signal chain as a voltage-divider cascade: transmitter-to-body return capacitance, body self-capacitance, and a receiver-side divider between the receiver's return capacitance, load capacitance, and parasitic pickup capacitance. `disc_electrode_cret(radius, thickness)` supplies the fringing "return" capacitance of a disc electrode used at either end of that chain.

### Locked Week 2 operating point

The Week 3 simulation is frozen against a single validated operating point rather than re-solving the physical models on every run (the functions above remain importable for future sensitivity sweeps):

| Parameter | Value |
|---|---|
| Frequency | 500 kHz |
| Primary TX power (Node A) | −10 dBm |
| A → B galvanic differential link gain | **−40.27 dB** |
| A → C capacitive cross-coupling leakage | **−15.15 dB** |

The core physical-layer finding that motivates Week 3 is visible directly in this table: the *unintended* capacitive path (−15.15 dB) is over 25 dB **less** attenuated than the *intended* differential path (−40.27 dB). Physical attenuation alone cannot secure the primary channel — hence the active masking architecture below.

---

## MAC Layer & Network Simulation (Week 3)

`week3_network_sim.py` adds a discrete-event packet layer on top of the frozen physical operating point:

- **Traffic model** — a single `simpy` transmitter process at Node A emits a configurable batch of fixed-size packets back-to-back (packet airtime derived from bitrate, plus an interpacket gap), each accompanied by a continuous PRBS-23 wideband masking carrier injected onto the capacitive cross-coupling path.
- **Channel dynamics** — each packet's link and leakage gains are independently perturbed by Gaussian jitter (in dB) to stand in for dry-contact impedance instability, and the masking carrier's coupling to Node B's common-mode node vs. its coupling to Node C is allowed to diverge slightly, since they are not literally the same physical measurement point.
- **Receiver noise** — computed from first principles via `kTB` at a configurable receiver bandwidth and noise figure, rather than an assumed constant.
- **Link-layer error model** — BPSK bit-error probability from the Gaussian Q-function, expanded to a per-packet delivery probability via `1 - (1 - BER)^packet_bits`. Swap `ber_bpsk()` for a different modem's BER(SINR) curve as needed.
- **CMRR realism** — `effective_system_cmrr_db()` derates the nominal amplifier CMRR by the *worse* of two independent mechanisms (see below), rather than trusting a flat datasheet number.

---

## Security Architecture

The active-masking scheme works by injecting a wideband PRBS-23 carrier onto the same capacitive path that leaks the primary signal to Node C, on the premise that Node B's differential front end will reject it (via CMRR) while Node C's single-ended pickup cannot. The simulator was used to pressure-test that premise through three progressively more rigorous scenarios.

### Scenario A — Ideal Datasheet (86 dB CMRR)

The amplifier's headline CMRR spec is taken at face value and applied flat across frequency, with perfectly symmetric common-mode coupling assumed at Node B.

| Metric | Value |
|---|---|
| Effective system CMRR | 86.0 dB |
| Node B mean SINR / PDR | 50.7 dB / **100.0%** |
| Node C mean SINR / PDR | 7.0 dB / **46.7%** |

Node B looks perfectly secure. Node C — the actual adversary — still decodes nearly half of all packets.

### Scenario B — Realistic Derated (46 dB CMRR)

Two independent, physically grounded mechanisms are used to derate the datasheet figure, and the simulator takes whichever is worse:

1. **Frequency rolloff** — CMRR falls off roughly 20 dB/decade above an assumed 10 kHz corner (typical instrumentation-amplifier behavior). At 500 kHz, that alone costs ~34 dB.
2. **Impedance-mismatch ceiling** — a first-order common-mode-to-differential-mode conversion limit, `CMRR_system ≈ 20·log₁₀(Z_in / ΔZ_electrode)`, driven by the impedance mismatch between Node B's two dry-contact electrodes. This is a *system*-level ceiling the amplifier's own datasheet CMRR cannot capture, and it does not improve with a better amplifier — it's set by the electrode-tissue interface, which for dry contact is both large and dynamically unstable.

| Metric | Value |
|---|---|
| Effective system CMRR | 46.0 dB |
| Node B mean SINR / PDR | 28.0 dB / **100.0%** |
| Node C mean SINR / PDR | 7.0 dB / **46.7%** |

Node B's margin drops by over 20 dB but its link survives comfortably. **Node C's numbers do not move at all** — confirming, quantitatively, that CMRR never enters Node C's channel: it only ever acted on Node B's reception.

### Scenario C — The Mitigated State (0 dB TX gap)

Formalizing the vulnerability makes the fix obvious. Because the primary signal and the masking carrier share the *same* leakage path to Node C, the path loss cancels out of Node C's SINR entirely:

```
SINR_C  =  TX_primary − TX_mask     (channel gain cancels — independent of CMRR, independent of attenuation)
```

With the original −10 dBm / −17 dBm split, that's a fixed 7 dB gap — survivable for a single-ended eavesdropper regardless of how good Node B's amplifier is. Closing the gap to 0 dB (masking power raised to match primary TX power, −10 dBm) and re-running under the *same* realistic 46 dB CMRR:

| Metric | Value |
|---|---|
| Effective system CMRR | 46.0 dB |
| Node B mean SINR / PDR | 21.1 dB / **99.5%** |
| Node C mean SINR / PDR | 0.0 dB / **0.0%** |

Node B loses another ~7 dB of margin but stays fully operational; Node C's link collapses. **The operative security parameter is the TX-power gap, not the CMRR spec.** CMRR determines how much masking-power headroom Node B's receiver can absorb without being desensitized — it never determines how much protection Node C is denied.

### Summary

| Scenario | System CMRR | Node B PDR | Node C PDR |
|---|---|---|---|
| A — Ideal datasheet | 86 dB | 100.0% | 46.7% |
| B — Realistic derated | 46 dB | 100.0% | 46.7% |
| C — Mitigated (0 dB TX gap) | 46 dB | 99.5% | 0.0% |

---

## Scope & Limitations

This is a link-budget and packet-delivery simulation, not a formal security proof. Before treating the mitigated state as an operational guarantee, note:

- **CMRR derating constants are illustrative, not measured.** The 10 kHz corner frequency and the electrode impedance mismatch (`Z_in`, `ΔZ`) are generic, textbook-order-of-magnitude placeholders — replace them with values characterized from your actual front-end and electrodes.
- **Dry-contact impedance is treated as a fixed mismatch plus Gaussian jitter.** Real dry-contact impedance can swing by orders of magnitude with pressure, sweat, and motion; a single "worst-case ΔZ" is a simplification, not a bound.
- **The eavesdropper model assumes a fixed, known leakage path and no adaptive demodulation.** A more capable adversary (e.g., one that estimates and subtracts the known PRBS-23 masking sequence, since PRBS is deterministic and public by construction if the polynomial is reused) is not modeled here.
- **BPSK/Q-function BER is a stand-in**, not the real modem's measured BER(SINR) curve.

---

## Roadmap

- [ ] Multi-hop / multi-transmitter MAC contention (this simulator currently models a single always-on transmitter).
- [ ] Replace the deterministic PRBS-23 masking assumption with a keyed, non-reusable sequence and model an adaptive eavesdropper.
- [ ] Swap in measured CMRR-vs-frequency and electrode-impedance data in place of the generic derating model.
- [ ] Sensitivity sweep across the full `channel_model.py` parameter space (electrode geometry, tissue conductivity) rather than the single frozen operating point.

## Contributing

Issues and pull requests are welcome. If you're proposing a change to the physical-layer constants or the CMRR derating model, please include the source (datasheet, measurement, or paper) for the new values.

## License

This project is released under the MIT License — see `LICENSE` for full text. (Add a `LICENSE` file with your chosen license before publishing; MIT is a common default for research/simulation tooling like this.)
