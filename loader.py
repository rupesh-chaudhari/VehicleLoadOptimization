"""Printable loader sheet (HTML) for one truck: step-by-step loading instructions + planogram."""
import html
import pandas as pd
from planogram import top_view, side_view, colour


def loading_steps(blocks: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for n, d in enumerate(blocks.sort_values("From (mm)").to_dict("records"), start=1):
        cpr = int(d["Cases per row"]); full, rem = divmod(int(d["Cases"]), cpr)
        orient = "Long side ALONG the truck" if d["Orientation"] == "Lengthwise" else "Long side ACROSS the truck"
        last = "" if rem == 0 else f"Last row: {rem} cases in {-(-rem // int(d['Layers']))} stacks (start from left wall)"
        rows.append({"Step": n, "Zone from cab (m)": f"{d['From (mm)'] / 1000:.2f} – {d['To (mm)'] / 1000:.2f}",
                     "Drop / stop": (f"Stop {d['Stop']} · {d['Drop']}" if d.get("Stop") else (d.get("Drop") or "-")),
                     "Category": d["Category"], "Case position": orient,
                     "Pattern": f"{int(d['Lanes'])} across × {int(d['Layers'])} high = {cpr} per row",
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
    side = side_view(plan["stacks"], t, by, names).to_html(full_html=False, include_plotlyjs=False)
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
<div class="pb"></div>{top}{side}
<div class="box"><div style="flex:1"><h3>Pick list</h3><table><tr><th>Category</th><th>Drop</th><th>Cases</th></tr>{lines}</table></div>
<div style="flex:1"><h3>Sign-off</h3><table class="sign"><tr><td>Loader</td><td></td></tr><tr><td>Supervisor</td><td></td></tr>
<tr><td>Vehicle no.</td><td></td></tr><tr><td>Seal no.</td><td></td></tr><tr><td>Time</td><td></td></tr></table></div></div>
<p style="color:#666;font-size:12px">Each tile in the top view = one stack; each band in the side view = one layer. Keep stacks within the stacking-layer norm.</p>
</body></html>"""
