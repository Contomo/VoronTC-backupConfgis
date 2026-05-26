import numpy as np
from scipy.optimize import minimize

OLD_CONFIG = {
    "linear_advance": 0.0193,
    "linearization_velocity": 1.0734,
}

TOWER = {
    "height_total": 25.0,
    "nlo_start": 0.0,
    "nlo_end": 0.5
}

V_SCV, V_SLOW, V_MED, V_FAST, V_ZERO = 5.0, 20.0, 120.0, 300.0, 0.0

raw_observations = [
    # --- LEFT FACE ---
    {"id": "L-Left",   "h": 9.56,  "moves": [(V_MED, V_SLOW)], "conf": 1.0},
    {"id": "L-Mid",    "h": 13.81, "moves": [(V_SLOW, V_SCV)], "conf": 1.0},
    {"id": "L-Right",  "h": 6.41,  "moves": [(V_SLOW, V_MED)], "conf": 1.0},

    # --- FRONT FACE ---
    # 9.7mm: Medium -> Stop -> Fast
    {"id": "F-Dwell",  "h": 9.70,  "moves": [(V_MED, V_ZERO), (V_ZERO, V_FAST)], "conf": 1.0},
    
    # Standard Front moves
    {"id": "F-Slow",   "h": 7.80,  "moves": [(V_FAST, V_SLOW)], "conf": 0.9},
    {"id": "F-Fast",   "h": 4.70,  "moves": [(V_SLOW, V_FAST)], "conf": 0.9},
    {"id": "F-SCV",    "h": 8.85,  "moves": [(V_FAST, V_SCV)],  "conf": 0.9},

    # --- RIGHT FACE ---
    # 12.5mm: Medium -> Stop -> Medium
    # This conflicts with F-Dwell (Decel part). Lowering confidence slightly.
    {"id": "R-Dwell",  "h": 12.50, "moves": [(V_MED, V_ZERO), (V_ZERO, V_MED)], "conf": 0.8},

    # --- BACK FACE ---
    # 10.58mm: Fast -> Stop -> Fast
    {"id": "B-Seam",   "h": 10.58, "moves": [(V_FAST, V_ZERO), (V_ZERO, V_FAST)], "conf": 0.8},
]

def get_nlo(h):
    return TOWER['nlo_start'] + (h / TOWER['height_total']) * (TOWER['nlo_end'] - TOWER['nlo_start'])

def recipr(v, v_lin):
    return v / (v + v_lin)

def calc_kick(v1, v2, la, nlo, v_lin):
    k_lin = la * (v2 - v1)
    s1, s2 = recipr(v1, v_lin), recipr(v2, v_lin)
    k_nonlin = nlo * (s2 - s1)
    return abs(k_lin + k_nonlin)

targets = []
print(f"{'ID':<14} | {'Sub-Event':<10} | {'Height':<6} | {'Conf':<4} | {'Target Kick'}")
print("-" * 65)

for obs in raw_observations:
    nlo = get_nlo(obs['h'])
    
    for i, (v1, v2) in enumerate(obs['moves']):
        sub_type = "Trans"
        if v2 == 0: sub_type = "Decel"
        elif v1 == 0: sub_type = "Accel"
        full_id = f"{obs['id']}-{sub_type}"
        
        tk = calc_kick(v1, v2, OLD_CONFIG['linear_advance'], nlo, OLD_CONFIG['linearization_velocity'])
        
        targets.append({
            "id": full_id,
            "v1": v1, "v2": v2,
            "target_kick": tk,
            "conf": obs['conf']
        })
        print(f"{full_id:<14} | {v1:.0f}->{v2:.0f} {'':<3} | {obs['h']:<6.2f} | {obs['conf']:<4.1f} | {tk:.6f}")

print("-" * 65)

def loss(params):
    la, nlo, v_lin = params
    if la < 0 or nlo < 0 or v_lin < 0.1: return 1e9
    
    total_loss = 0
    for t in targets:
        nk = calc_kick(t['v1'], t['v2'], la, nlo, v_lin)
        err_sq = ((nk - t['target_kick']) / (t['target_kick'] + 1e-9)) ** 2
        weighted_err = err_sq * (t['conf'] ** 2)
        total_loss += weighted_err
    return total_loss

res = minimize(loss, [OLD_CONFIG['linear_advance'], 0.2, OLD_CONFIG['linearization_velocity']], method='Nelder-Mead', tol=1e-8)
new_la, new_nlo, new_vlin = res.x

print("\n[extruder]")
print(f"pressure_advance_model: recipr")
print(f"linear_advance: {new_la:.6f}")
print(f"nonlinear_offset: {new_nlo:.6f}")
print(f"linearization_velocity: {new_vlin:.6f}")

#print("\nFIT ACCURACY (Weighted):")
#print(f"{'ID':<15} | {'Match %':<8} | {'Status'}")
#print("-" * 45)
#for t in targets:
#    nk = calc_kick(t['v1'], t['v2'], new_la, new_nlo, new_vlin)
#    diff = abs(nk - t['target_kick'])
#    match = 100 - (diff / t['target_kick'] * 100)
#    
#    status = ""
#    if match < 98:
#        if t['conf'] < 1.0: status = "deviation (low weight)"
#        else: status = "<< POOR FIT"
#    
#    print(f"{t['id']:<15} | {match:5.1f}%   | {status}")