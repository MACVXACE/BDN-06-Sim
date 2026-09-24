from __future__ import annotations
from dataclasses import dataclass
import numpy as np

EPS_0 = 8.854e-12  # vacuum permittivity, F/m

def disc_electrode_cret(radius_m: float, thickness_m: float) -> float:
    r, h = radius_m, thickness_m
    return 8 * EPS_0 * r * (1 + 0.87 * (h / (2 * r)) ** 0.76)

@dataclass
class CapacitiveHBCLink:
    cret_tx: float
    cret_rx: float
    c_body: float
    c_l: float
    c_pp: float

    def voltage_gain(self) -> float:
        v_body_over_vtx = self.cret_tx / self.c_body
        rx_divider = self.cret_rx / (self.cret_rx + self.c_l + self.c_pp)
        return v_body_over_vtx * rx_divider

    def gain_db(self) -> float:
        return 20 * np.log10(abs(self.voltage_gain()))

def skin_impedance_elements(
    sigma_skin_s_per_m: float,
    eps_r_skin: float,
    t_skin_m: float,
    r_m: float,
) -> tuple[float, float]:
    area = np.pi * r_m ** 2
    r_skin = t_skin_m / (sigma_skin_s_per_m * area)
    c_skin = (eps_r_skin * EPS_0) * area / t_skin_m
    return r_skin, c_skin

def muscle_resistance(
    sigma_muscle_s_per_m: float,
    h_muscle_m: float,
    s_m: float,
    r_m: float,
) -> float:
    rho_muscle = 1.0 / sigma_muscle_s_per_m
    return (rho_muscle / (2 * np.pi * h_muscle_m)) * np.log((s_m ** 2) / (r_m ** 2))

def sphere_sphere_fringing_leakage_db(
    r_m: float,
    dcc_m: float,
    freq_hz: float,
    z_load_ohm: float,
) -> tuple[float, float, float]:
    cf = 4 * np.pi * EPS_0 * (r_m ** 2) / dcc_m
    zf = 1.0 / (2 * np.pi * freq_hz * cf)
    ratio = (z_load_ohm / zf) ** 2
    ratio_db = 10 * np.log10(ratio)
    return cf, zf, ratio_db

def galvanic_symmetric_gain_db(
    r_muscle: float,
    r_skin: float,
    c_skin: float,
    freq_hz: float,
    electrode_spacing_m: float,
    link_distance_m: float,
    angle_rad: float,
    electrode_radius_m: float,
) -> float:
    f = freq_hz
    s, d, theta, r = electrode_spacing_m, link_distance_m, angle_rad, electrode_radius_m

    term1 = 20 * np.log10(r_muscle / (2 * r_skin))
    term2 = 10 * np.log10(
        (1 + (2 * np.pi * f * c_skin * r_skin) ** 2)
        / (1 + (np.pi * f * c_skin * r_muscle) ** 2)
    )
    inner = np.sqrt((1 + s**2 / d**2) ** 2 - (2 * s / d * np.sin(theta)) ** 2)
    term3 = 20 * np.log10(abs(np.log(inner) / np.log(s**2 / r**2)))
    return term1 + term2 + term3
