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
