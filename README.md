# Truck Load Planner (Streamlit)

Optimises vehicle loading, recommends the vehicle mix with the best utilisation (not always the
biggest or the smallest truck) and gives loaders a clear planogram and a printable loading sheet.

## Run
```
pip install -r requirements.txt
streamlit run app.py
```
Opens at http://localhost:8501.

## Input workbook (`load_planner_inputs.xlsx`)
| Sheet | Columns |
|---|---|
| Order | Drop (optional), Stop sequence (optional), Category, Order qty (cases), Priority, Must-load min (optional) |
| Category norms | Category, Case weight (kg), Stacking layers, Case length / width / height (mm) |
| Truck master | Truck type, Body type, Internal length / width (mm), Usable stack height (mm), Capacity (MT), Available for planning (Y/N) |
| Settings | Clearances (mm), default truck, target vehicle utilisation (%), vehicle count penalty |

Upload your own copy in the sidebar; the bundled template is used otherwise.
Case dimensions in the template are estimates - replace them with actual case sizes.

## Tabs
- **Recommended vehicles** - picks the truck mix that carries the whole order with the least deployed
  capacity (= highest utilisation). The *Vehicle choice* slider adds a penalty per vehicle:
  left = tightest fit (more, smaller trucks), right = fewer, bigger trucks. Trucks below the
  utilisation target are flagged with how much more would fit.
- **Load a chosen truck** - you pick the vehicle; objectives: Maximise cases, Priority order,
  Proportional (fair share), Maximise payload. Optional split over several trucks.
- **Loadability matrix** - max cases of each category per truck.
- **Category norms & trucks** - edit inputs for the session.

Every truck shows: utilisation, planogram (top view = stacks, side view = layers, blocks labelled
with category, lanes × layers and rows), numbered loading steps from the cab to the doors, a 3D
view, and a **printable loader sheet** (HTML: steps, planogram, pick list, sign-off; works offline).

**Multi-drop:** fill Drop + Stop sequence in the Order. Stop 1 is unloaded first, so it is loaded
last (at the doors); the last stop goes nearest the cab.

## Model
Mixed-integer programmes solved exactly with SciPy's HiGHS solver:
- `optimizer.py` - single-truck loading (rows of lanes × stacking layers, lengthwise or turned 90°;
  limits: floor length, payload, stacking norms, truck height) and the fleet recommendation
  (minimise Σ trucks × (capacity + vehicle penalty), then each truck loaded exactly and
  poorly-used trucks consolidated).
- `planogram.py` - top / side / 3D views; `loader.py` - loading steps and loader sheet;
  `make_template.py` - rebuilds the input template.

## Stable loading (transit-safe) – default ON
Sidebar toggle. Rules the planner follows so cases cannot shift forward, backward or sideways:
1. **One level load height for the whole truck** – every truck is planned to a common height (sidebar
   "Level load height", 0 = auto = case-weighted median of the products' maximum stack heights). No block may
   tower above it; the planner then searches the narrowest height window that still fits the body.
2. **Even stacks** – cases of a line are spread evenly, so stacks differ by at most one case; the higher
   stacks are full rows at the cab side of the block (the load steps *down* towards the doors, never up).
3. **Low-norm products are tiered within the same stop** – e.g. CAN 300ml (6 layers = 720 mm) goes as a flat
   base under, or on a tier sheet over, another product of the same stop, within the base case's stacking norm
   (crush) limit, so the column reaches the common level. Stops are never mixed.
4. **Full-width rows only**, one orientation per category, stacks centred (side gaps split both sides).
5. **Separator between stops** (corrugated/ply sheet + load bar), marked as a dashed line on the planogram.
6. **Floor reserve** – the optimiser keeps 4 % of the floor free so the layout has room to level the load.
7. **Stability panel + loader sheet** – height range, void at doors, side gap, floor gaps, centre of gravity
   and securing actions.
Turn the toggle off to see the maximum-cube layout for comparison.

## Full-layer rounding (decision point)
Below each plan: one row per line per truck with
*Cases per full layer* (lanes × rows of that block), *Part-layer cases*, **Add for full layer** (+ weight, height after,
does it fit the truck's spare payload) and **Remove for full layer** (height after), plus a *Suggested* action.
Pick Keep / Add / Remove in the *Decision* column → **Apply my decisions & re-plan** (or **Use suggestions**).
The adjusted quantities show above the tabs ("Reset to original order" undoes). Re-planning can create a few new
part layers – apply suggestions again (usually 2–3 rounds give every block full layers). Also exported as the
"Full-layer rounding" sheet in the Excel plan.
