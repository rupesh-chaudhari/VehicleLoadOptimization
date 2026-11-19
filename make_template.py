"""Creates load_planner_inputs.xlsx - the input template the app reads."""
import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.worksheet.datavalidation import DataValidation

NORMS = pd.DataFrame([
    ("200ml Fizz", 5.7, 7, 330, 225, 165), ("Swing 250ml PET", 8.8, 6, 375, 315, 185), ("ZS 600ml", 16.5, 6, 435, 290, 235),
    ("Swing 140ml", 7.7, 9, 360, 270, 125), ("PB-215ML", 10, 8, 400, 300, 190), ("Paper Boat Others", 10, 7, 400, 300, 190),
    ("Swing 600Ml", 15.4, 5, 435, 290, 235), ("Swing 1.2Ltrs", 17.6, 5, 360, 270, 315), ("Swing 270ml PET", 9.3, 7, 380, 315, 195),
    ("CAN 300ml", 8.6, 6, 400, 265, 120)],
    columns=["Category", "Case weight (kg)", "Stacking layers", "Case length (mm)", "Case width (mm)", "Case height (mm)"])
TRUCKS = pd.DataFrame([
    ("14 ft Open (LCV, 4 T)", "Open body", 4270, 1980, 1800, 4), ("17 ft Open (7 T)", "Open body", 5180, 2130, 2000, 7),
    ("19 ft Open (9 T)", "Open body", 5790, 2290, 2100, 9), ("22 ft Open (12 T)", "Open body", 6710, 2290, 2200, 12),
    ("22 ft Open 10-wheeler (16 T)", "Open body", 6710, 2290, 2300, 16), ("24 ft Open 10-wheeler (21 T)", "Open body", 7320, 2440, 2400, 21),
    ("28 ft Open 12-wheeler (25 T)", "Open body", 8530, 2440, 2400, 25), ("32 ft Open 14-wheeler (30 T)", "Open body", 9750, 2440, 2400, 30),
    ("14 ft Container (4 T)", "Container", 4270, 1980, 1980, 4), ("20 ft Container (7 T)", "Container", 6100, 2290, 2290, 7),
    ("32 ft SXL Container (9 T)", "Container", 9750, 2440, 2440, 9), ("24 ft Container (10 T)", "Container", 7320, 2440, 2440, 10),
    ("32 ft MXL Container (18 T)", "Container", 9750, 2440, 2440, 18), ("32 ft MXL Container (21 T)", "Container", 9750, 2440, 2440, 21),
    ("32 ft MXL High-cube Container (23 T)", "Container", 9750, 2440, 2740, 23), ("34 ft MXL High-cube Container (25 T)", "Container", 10360, 2440, 2740, 25),
    ("40 ft Trailer Container (27 T)", "Container", 12030, 2350, 2390, 27)],
    columns=["Truck type", "Body type", "Internal length (mm)", "Internal width (mm)", "Usable stack height (mm)", "Capacity (MT)"])
TRUCKS["Available for planning (Y/N)"] = "Y"
ORDER = pd.DataFrame([
    ("Whitefield DT", 1, "ZS 600ml", 120, 1), ("Whitefield DT", 1, "Swing 250ml PET", 150, 2),
    ("Electronic City MT", 2, "CAN 300ml", 200, 3), ("Electronic City MT", 2, "200ml Fizz", 250, 4), ("Electronic City MT", 2, "Swing 1.2Ltrs", 60, 5),
    ("Hosur DT", 3, "Swing 250ml PET", 180, 6), ("Hosur DT", 3, "PB-215ML", 100, 7),
    ("Kolar DT", 4, "ZS 600ml", 90, 8), ("Kolar DT", 4, "Swing 140ml", 150, 9),
    ("Tumakuru DT", 5, "200ml Fizz", 200, 10), ("Tumakuru DT", 5, "Swing 600Ml", 80, 11),
    ("Mandya DT", 6, "Swing 270ml PET", 160, 12), ("Mandya DT", 6, "CAN 300ml", 100, 13),
    ("Mysuru DT", 7, "ZS 600ml", 150, 14), ("Mysuru DT", 7, "Swing 250ml PET", 200, 15), ("Mysuru DT", 7, "200ml Fizz", 250, 16),
    ("Hassan DT", 8, "Swing 1.2Ltrs", 80, 17), ("Hassan DT", 8, "Paper Boat Others", 120, 18)],
    columns=["Drop", "Stop sequence", "Category", "Order qty (cases)", "Priority"])
ORDER["Must-load min (cases)"] = None
SETTINGS = pd.DataFrame([("Length clearance (mm)", 50), ("Width clearance (mm)", 20), ("Height clearance (mm)", 50),
                         ("Default truck type", "Container"), ("Default capacity (MT)", 18),
                         ("Target vehicle utilisation (%)", 85), ("Vehicle count penalty (T-equivalent)", 4)],
                        columns=["Parameter", "Value"])

def build(path="load_planner_inputs.xlsx"):
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        ORDER.to_excel(w, sheet_name="Order", index=False); NORMS.to_excel(w, sheet_name="Category norms", index=False)
        TRUCKS.to_excel(w, sheet_name="Truck master", index=False)
        SETTINGS.to_excel(w, sheet_name="Settings", index=False)
    wb = load_workbook(path)
    yel = PatternFill("solid", start_color="FFF2CC"); hdr = PatternFill("solid", start_color="D9E1F2")
    for ws in wb:
        for c in ws[1]:
            c.font = Font(name="Arial", bold=True); c.fill = hdr; c.alignment = Alignment(wrap_text=True, vertical="center")
        for row in ws.iter_rows(min_row=2):
            for c in row:
                c.font = Font(name="Arial", color="0000FF"); c.fill = yel
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width = max(12, min(40, max(len(str(c.value or "")) for c in col) + 2))
        ws.row_dimensions[1].height = 30; ws.freeze_panes = "A2"
    n = wb["Category norms"]
    n.cell(len(NORMS) + 3, 1, "Case weight & stacking layers: your stacking norms. Case dimensions are ESTIMATES - replace with actual case sizes (length = longer footprint side).").font = Font(name="Arial", italic=True, color="C00000")
    o = wb["Order"]
    dv = DataValidation(type="list", formula1=f"='Category norms'!$A$2:$A${len(NORMS) + 1}", allow_blank=True)
    o.add_data_validation(dv); dv.add("C2:C200")
    o.cell(1, 8, "One row per order line. Drop + Stop sequence are optional (multi-drop trucks: stop 1 is unloaded first, so it is loaded last at the doors). Priority 1 = load first. Must-load min is optional.").font = Font(name="Arial", italic=True)
    t = wb["Truck master"]
    t.cell(len(TRUCKS) + 3, 1, "Set 'Available for planning' to N for vehicles you do not want recommended. Dimensions are typical - verify with your transporters.").font = Font(name="Arial", italic=True, color="C00000")
    wb.save(path)


if __name__ == "__main__":
    build()
