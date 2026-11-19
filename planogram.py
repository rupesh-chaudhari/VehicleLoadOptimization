"""Plotly planogram views of a load plan."""
import plotly.graph_objects as go
import pandas as pd

PALETTE = ["#1F77B4", "#FF7F0E", "#2CA02C", "#D62728", "#9467BD", "#8C564B", "#E377C2", "#7F7F7F", "#BCBD22", "#17BECF"]


def colour(line: int) -> str:
    return PALETTE[(int(line) - 1) % len(PALETTE)]


def _groups(stacks: pd.DataFrame, by: str, names: dict | None = None):
    """Yield (key, label, frame) for colouring by order line or by milk-run stop."""
    for key, g in stacks.groupby(by):
        if by == "Stop":
            label = f"Stop {key}: {(names or {}).get(g.Drop.iloc[0], g.Drop.iloc[0])}"
        else:
            label = f"{key}. {g.Category.iloc[0]}"
        yield key, label, g


def _frame(fig, L, W_or_H, title, ylab):
    fig.add_shape(type="rect", x0=0, y0=0, x1=L, y1=W_or_H, line=dict(color="#333", width=2))
    fig.update_layout(title=title, height=330, margin=dict(l=40, r=20, t=50, b=40), plot_bgcolor="white",
                      showlegend=True, legend=dict(orientation="h", y=-0.25))
    fig.update_xaxes(title="Length from cab (mm)  ◄ FRONT · REAR ►", range=[-100, L + 100], showgrid=False)
    fig.update_yaxes(title=ylab, range=[-80, W_or_H * 1.08], scaleanchor="x", scaleratio=1, showgrid=False)


def top_view(stacks: pd.DataFrame, blocks: pd.DataFrame, truck, by: str = "Line", names: dict | None = None) -> go.Figure:
    fig = go.Figure()
    for line, label, g in _groups(stacks, by, names):
        xs, ys, text = [], [], []
        for _, s in g.iterrows():
            xs += [s.x, s.x + s.dx, s.x + s.dx, s.x, s.x, None]
            ys += [s.y, s.y, s.y + s.dy, s.y + s.dy, s.y, None]
        fig.add_trace(go.Scatter(x=xs, y=ys, fill="toself", mode="lines", line=dict(color="white", width=1),
                                 fillcolor=colour(line), name=label, hoverinfo="name"))
    for _, b in blocks.iterrows():
        k = b[by]; mid = (b["From (mm)"] + b["To (mm)"]) / 2
        width_mm = b["To (mm)"] - b["From (mm)"]
        tag = f"S{k}" if by == "Stop" else f"{k}"
        fig.add_annotation(x=mid, y=truck.width + 70, showarrow=False, text=f"<b>{tag}</b>", font=dict(size=11, color=colour(k)))
        if width_mm > truck.length * 0.07:
            fig.add_annotation(x=mid, y=truck.width / 2, showarrow=False, bgcolor="rgba(255,255,255,0.85)", borderpad=2,
                               text=f"<b>{b.Category[:16]}</b><br>{b.Lanes}×{b.Layers} · {b.Rows} rows<br>{b.Cases} cs", font=dict(size=9, color="#222"))
    _frame(fig, truck.length, truck.width, "Top view - each tile is one stack of cases", "Width (mm)")
    return fig


def side_view(stacks: pd.DataFrame, truck, by: str = "Line", names: dict | None = None) -> go.Figure:
    fig = go.Figure()
    # tallest stack per row position (what you see from the side)
    side = stacks.sort_values("z").groupby(["x", "Line"], as_index=False).last()
    for line, label, g in _groups(side, by, names):
        xs, ys = [], []
        for _, s in g.iterrows():
            for k in range(int(s.layers)):
                y0 = k * s.h
                xs += [s.x, s.x + s.dx, s.x + s.dx, s.x, s.x, None]; ys += [y0, y0, y0 + s.h, y0 + s.h, y0, None]
        fig.add_trace(go.Scatter(x=xs, y=ys, fill="toself", mode="lines", line=dict(color="white", width=1),
                                 fillcolor=colour(line), name=label, hoverinfo="name"))
    _frame(fig, truck.length, truck.height, "Side view - each band is one layer", "Height (mm)")
    return fig


def view_3d(stacks: pd.DataFrame, truck, by: str = "Line", names: dict | None = None) -> go.Figure:
    fig = go.Figure()
    faces = [(0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    for line, label, g in _groups(stacks, by, names):
        X, Y, Z, I, J, K = [], [], [], [], [], []
        ex, ey, ez = [], [], []
        for _, s in g.iterrows():
            b = len(X); x0, x1, y0, y1, z1 = s.x, s.x + s.dx, s.y, s.y + s.dy, s.z
            X += [x0, x1, x1, x0, x0, x1, x1, x0]; Y += [y0, y0, y1, y1, y0, y0, y1, y1]; Z += [0, 0, 0, 0, z1, z1, z1, z1]
            for f in faces:
                I.append(b + f[0]); J.append(b + f[1]); K.append(b + f[2])
            for (a, c) in ((x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)):
                ex.append(a); ey.append(c); ez.append(z1)
            ex.append(None); ey.append(None); ez.append(None)
        fig.add_trace(go.Mesh3d(x=X, y=Y, z=Z, i=I, j=J, k=K, color=colour(line), opacity=1, flatshading=True,
                                name=label, showlegend=True, hoverinfo="name"))
        fig.add_trace(go.Scatter3d(x=ex, y=ey, z=ez, mode="lines", line=dict(color="white", width=2), showlegend=False, hoverinfo="skip"))
    L, W, H = truck.length, truck.width, truck.height
    bx = [0, L, L, 0, 0, None, 0, L, L, 0, 0, None, 0, 0, None, L, L, None, L, L, None, 0, 0]
    by = [0, 0, W, W, 0, None, 0, 0, W, W, 0, None, 0, 0, None, 0, 0, None, W, W, None, W, W]
    bz = [0, 0, 0, 0, 0, None, H, H, H, H, H, None, 0, H, None, 0, H, None, 0, H, None, 0, H]
    fig.add_trace(go.Scatter3d(x=bx, y=by, z=bz, mode="lines", line=dict(color="#555", width=3), name="Truck body"))
    fig.update_layout(height=520, margin=dict(l=0, r=0, t=30, b=0), legend=dict(orientation="h"),
                      scene=dict(aspectmode="data", xaxis_title="Length (mm)", yaxis_title="Width (mm)", zaxis_title="Height (mm)",
                                 camera=dict(eye=dict(x=-1.3, y=-1.6, z=0.9))))
    return fig

