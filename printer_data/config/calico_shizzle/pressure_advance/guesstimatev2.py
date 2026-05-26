import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import cm
from scipy.optimize import minimize, minimize_scalar

# ==========================================
# 1. DATA SETUP (Combined Tower 1 & 2)
# ==========================================
targets = []

# Tower 1 (Baseline)
c1_la, c1_vlin = 0.0193, 1.0734
def get_nlo_t1(h): return 0.0 + (h/25.0)*0.5

t1_obs = [
    {"v1": 120, "v2": 20, "h": 9.56}, {"v1": 20, "v2": 5, "h": 13.81},
    {"v1": 20, "v2": 120, "h": 6.41}, {"v1": 120, "v2": 0, "h": 9.70},
    {"v1": 300, "v2": 20, "h": 7.80}, {"v1": 20, "v2": 300, "h": 4.70},
    {"v1": 300, "v2": 5, "h": 8.85}, {"v1": 120, "v2": 0, "h": 12.50},
    {"v1": 300, "v2": 0, "h": 10.58}
]
for o in t1_obs:
    v1, v2 = o['v1'], o['v2']
    s1=v1/(v1+c1_vlin); s2=v2/(v2+c1_vlin)
    tk = abs(c1_la*(v2-v1) + get_nlo_t1(o['h'])*(s2-s1))
    targets.append({"v1": v1, "v2": v2, "tk": tk})

# Tower 2 (Refinement)
c2_la, c2_vlin = 0.019249, 1.712485
def get_nlo_t2(h): return 0.141 + (h/25.0)*(0.262-0.141)

t2_obs = [
    {"v1": 120, "v2": 20, "h": 8.36}, {"v1": 120, "v2": 0, "h": 10.29},
    {"v1": 300, "v2": 20, "h": 2.0},  {"v1": 300, "v2": 5, "h": 10.3}
]
for o in t2_obs:
    v1, v2 = o['v1'], o['v2']
    s1=v1/(v1+c2_vlin); s2=v2/(v2+c2_vlin)
    tk = abs(c2_la*(v2-v1) + get_nlo_t2(o['h'])*(s2-s1))
    targets.append({"v1": v1, "v2": v2, "tk": tk})

# ==========================================
# 2. SOLVER LOGIC
# ==========================================

def calc_error_fixed(la, nlo, v_lin):
    err = 0
    for t in targets:
        v1, v2 = t['v1'], t['v2']
        s1 = v1/(v1+v_lin); s2 = v2/(v2+v_lin)
        k = abs(la*(v2-v1) + nlo*(s2-s1))
        err += ((k - t['tk']) / (t['tk']+1e-9))**2
    return err

def get_best_error_for_pixel(nlo, v_lin):
    # This is the "Projection": Find the best LA for this specific NLO/V_lin combo
    res = minimize_scalar(
        lambda la: calc_error_fixed(la, nlo, v_lin), 
        bounds=(0.015, 0.025), 
        method='bounded'
    )
    return res.fun  # Return the minimum error found

# ==========================================
# 3. GLOBAL OPTIMIZATION (To find the White Dot)
# ==========================================
print("Finding Global Minimum (3-Variable Solve)...")
res_global = minimize(
    lambda p: calc_error_fixed(p[0], p[1], p[2]), 
    [0.0185, 0.25, 1.0], 
    method='Nelder-Mead', tol=1e-9
)
BEST_LA, BEST_NLO, BEST_VLIN = res_global.x
MIN_ERR = res_global.fun
print(f"Global Min: LA={BEST_LA:.6f}, NLO={BEST_NLO:.6f}, VLIN={BEST_VLIN:.6f}")

# ==========================================
# 4. GENERATE LANDSCAPE
# ==========================================
print("Generating Projected Landscape (Solving LA for every pixel)...")
# X: Linearization Velocity (Shape)
x_vlin = np.linspace(0.5, 2.5, 50)
# Y: Nonlinear Offset (Magnitude)
y_nlo = np.linspace(0.15, 0.45, 50)

X, Y = np.meshgrid(x_vlin, y_nlo)
Z = np.zeros_like(X)

for i in range(X.shape[0]):
    for j in range(X.shape[1]):
        Z[i, j] = get_best_error_for_pixel(Y[i, j], X[i, j])

# ==========================================
# 5. PLOT
# ==========================================
fig = plt.figure(figsize=(12, 10))
ax = fig.add_subplot(111, projection='3d')

surf = ax.plot_surface(X, Y, Z, cmap=cm.viridis, linewidth=0, antialiased=False, alpha=0.9)

# Mark Global Min
ax.scatter(BEST_VLIN, BEST_NLO, MIN_ERR, color='white', s=200, edgecolors='black', zorder=10, label='Global Minimum')

# Mark Gen 1 (Recalculated with its specific LA to show how bad it was)
err_gen1 = calc_error_fixed(0.01925, 0.201, 1.71)
ax.scatter(1.71, 0.201, err_gen1, color='red', s=200, edgecolors='white', zorder=10, label='Gen 1 (The Trap)')

ax.set_xlabel('Linearization Velocity (Shape)')
ax.set_ylabel('Nonlinear Offset (Magnitude)')
ax.set_zlabel('Projected Error (Best Possible LA)')
ax.set_title('True Pressure Advance Landscape\n(Z = Error with Optimized LA)')

# View Angle
ax.view_init(elev=40, azim=-30)

plt.legend()
plt.savefig('true_landscape.png', dpi=150)
print("[DONE] Saved to true_landscape.png")