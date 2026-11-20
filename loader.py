"""Printable loader sheet (HTML) for one truck: step-by-step loading instructions + planogram."""
import html
import pandas as pd
from planogram import top_view, side_view, colour
from optimizer import stability_report


def loading_steps(blocks: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for n, d in enumerate(blocks.sort_values(["From (mm)", "Tier"] if "Tier" in blocks else "From (mm)").to_dict("records"), start=1):
        cpr = int(d["Cases per row"])
        orient = "Long side ALONG the truck" if d["Orientation"] == "Lengthwise" else "Long side ACROSS the truck"
        last = d.get("Note") or ""
        rows.append({"Step": n, "Zone from cab (m)": f"{d['From (mm)'] / 1000:.2f} – {d['To (mm)'] / 1000:.2f}",
                     "Drop / stop": (f"Stop {d['Stop']} · {d['Drop']}" if d.get("Stop") else (d.get("Drop") or "-")),
                     "Category": d["Category"], "Case position": orient,
                     "Pattern": f"{int(d['Lanes'])} across × {int(d['Layers'])} high = {cpr} per row"
                                + (f" (centre; {int(d['Side gap each side (mm)'])} mm gap each side)" if d.get("Side gap each side (mm)", 0) > 40 else ""),
                     "Rows": int(d["Rows"]), "Cases": int(d["Cases"]), "Note": last, "_key": d["Stop"] if d.get("Stop") else d["Line"]})
    return pd.DataFrame(rows)


def loader_html(plan: dict, title: str, by: str = "Line", names: dict | None = None) -> str:
    t, k = plan["truck"], plan["kpi"]
    steps = loading_steps(plan["blocks"])
    trs = "".join(
        f"<tr><td style='background:{colour(r['_key'])};color:#fff;font-weight:700;text-align:center'>{r['Step']}</td>"
        + "".join(f"<td>{html.escape(str(r[c]))}</td>" for c in ["Zone from cab (m)", "Drop / stop", "Category", "Case position", "Pattern", "Rows", "Cases", "Note"])
        + "</tr>" for _, r in steps.iterrows())
    top = top_view(plan["stacks"], plan["blocks"], t, by, names).to_html(full_html=False, include_plotlyjs=True)  # embedded: prints offline
    side = side_view(plan["stacks"], t, by, names, plan["blocks"]).to_html(full_html=False, include_plotlyjs=False)
    rep = stability_report(plan)
    acts = "".join(f"<li>{html.escape(a)}</li>" for a in rep.get("actions", [])) or "<li>No extra securing needed beyond standard practice.</li>"
    stab = (f"<h3>Securing the load</h3><p>Load height {rep.get('height_min', 0):.0f}–{rep.get('height_max', 0):.0f} mm · "
            f"void at doors {rep.get('void_mm', 0) / 1000:.2f} m · side gap ≤ {rep.get('side_gap_mm', 0):.0f} mm each side · "
            f"centre of gravity {rep.get('cg_pct', 0):.0%} of body length</p><ul>{acts}"
            "<li>Every row is full width from wall to wall; part layers only on top. Do not leave gaps on the floor.</li>"
            "<li>One level load height cab to doors - do not stack any block higher than shown, even if space is left.</li><li>Fix a separator sheet + load bar at every dashed separator line before loading the next stop.</li></ul>")
    tot = plan["table"]
    lines = "".join(f"<tr><td>{html.escape(str(r['Category']))}</td><td>{html.escape(str(r['Drop'] or '-'))}</td>"
                    f"<td style='text-align:right'>{int(r['Loaded cases']):,}</td></tr>" for _, r in tot[tot["Loaded cases"] > 0].iterrows())
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>body{{font-family:Arial,sans-serif;margin:24px;color:#222}} h1{{font-size:22px;margin:0}} .k{{color:#555;margin:6px 0 16px}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0 18px}} th,td{{border:1px solid #bbb;padding:6px 8px;vertical-align:top}}
th{{background:#D9E1F2;text-align:left}} .box{{display:flex;gap:28px}} .sign td{{height:34px}} @media print{{.pb{{page-break-before:always}}}}</style></head><body>
<h1>{html.escape(title)} — loading sheet</h1>
<div class="k"><b>{html.escape(t.name)}</b> · usable {t.length:,.0f} × {t.width:,.0f} × {t.height:,.0f} mm ·
<b>{k['cases']:,} cases</b> · {k['weight_kg'] / 1000:.2f} MT ({k['payload_util']:.0%} payload) · floor {k['floor_util']:.0%}</div>
<h3>Load in this order — start at the cab end, finish at the doors</h3>
<table><tr><th>Step</th><th>Zone from cab (m)</th><th>Drop / stop</th><th>Category</th><th>Case position</th><th>Pattern</th><th>Rows</th><th>Cases</th><th>Note</th></tr>{trs}</table>
{stab}<div class="pb"></div>{top}{side}
<div class="box"><div style="flex:1"><h3>Pick list</h3><table><tr><th>Category</th><th>Drop</th><th>Cases</th></tr>{lines}</table></div>
<div style="flex:1"><h3>Sign-off</h3><table class="sign"><tr><td>Loader</td><td></td></tr><tr><td>Supervisor</td><td></td></tr>
<tr><td>Vehicle no.</td><td></td></tr><tr><td>Seal no.</td><td></td></tr><tr><td>Time</td><td></td></tr></table></div></div>
<p style="color:#666;font-size:12px">Each tile in the top view = one stack; each band in the side view = one layer. Keep stacks within the stacking-layer norm.</p>
</body></html>"""
