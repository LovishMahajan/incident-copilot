// ADR-70 demo viewer: the page's icons. Static shapes authored here, built with
// createElementNS (no HTML parsing, no icon font, nothing fetched). API data never reaches
// this module: callers pass an icon name from their own constants.

const NS = "http://www.w3.org/2000/svg";

// Each icon is a list of [tag, attributes] on a 24x24 grid, drawn with the current colour.
const SHAPES = {
  logo: [
    ["path", { d: "M12 2.5 20.5 7v10L12 21.5 3.5 17V7z" }],
    ["path", { d: "M8 12.5l2.6 2.6L16 9.5" }],
  ],
  incidents: [
    ["path", { d: "M12 3 2.5 20h19z" }],
    ["path", { d: "M12 10v4.5" }],
    ["path", { d: "M12 17.4v.1" }],
  ],
  search: [
    ["circle", { cx: 11, cy: 11, r: 7 }],
    ["path", { d: "m20.5 20.5-4.5-4.5" }],
  ],
  sun: [
    ["circle", { cx: 12, cy: 12, r: 4 }],
    ["path", { d: "M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" }],
  ],
  moon: [["path", { d: "M20.5 14.5A8.5 8.5 0 1 1 9.5 3.5a7 7 0 0 0 11 11z" }]],
  monitor: [
    ["rect", { x: 3, y: 4, width: 18, height: 12, rx: 2 }],
    ["path", { d: "M8 20h8M12 16v4" }],
  ],
  chevronDown: [["path", { d: "m6 9 6 6 6-6" }]],
  chevronRight: [["path", { d: "m9 6 6 6-6 6" }]],
  check: [["path", { d: "M5 12.5l4.5 4.5L19 7.5" }]],
  x: [["path", { d: "M6 6l12 12M18 6 6 18" }]],
  warn: [
    ["path", { d: "M12 3 2.5 20h19z" }],
    ["path", { d: "M12 10v4M12 17v.1" }],
  ],
  info: [
    ["circle", { cx: 12, cy: 12, r: 9 }],
    ["path", { d: "M12 11v5M12 8v.1" }],
  ],
  cube: [
    ["path", { d: "M12 2.8 20 7.2v9.6L12 21.2 4 16.8V7.2z" }],
    ["path", { d: "M4 7.2l8 4.4 8-4.4M12 11.6v9.6" }],
  ],
  clock: [
    ["circle", { cx: 12, cy: 12, r: 9 }],
    ["path", { d: "M12 7v5l3 2" }],
  ],
  external: [
    ["path", { d: "M14 4h6v6M20 4l-9 9" }],
    ["path", { d: "M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5" }],
  ],
  wrench: [
    [
      "path",
      {
        d: "M14.7 6.3a4 4 0 0 0 5 5L21 13l-1.5 1.5a6 6 0 0 1-6.6 1.3l-6 6a2.1 2.1 0 0 1-3-3l6-6a6 6 0 0 1 1.3-6.6L12.7 3z",
      },
    ],
  ],
  sparkle: [["path", { d: "M12 3l1.9 5.6L19.5 10.5 13.9 12.4 12 18l-1.9-5.6L4.5 10.5l5.6-1.9z" }]],
  menu: [["path", { d: "M4 6h16M4 12h16M4 18h16" }]],
  play: [["path", { d: "M7 4.5v15l12-7.5z" }]],
  pulse: [["path", { d: "M3 12h4l2.5-6 5 12 2.5-6h4" }]],
  lock: [
    ["rect", { x: 5, y: 11, width: 14, height: 10, rx: 2 }],
    ["path", { d: "M8 11V8a4 4 0 0 1 8 0v3" }],
  ],
  flag: [["path", { d: "M5 21V4M5 4h11l-2 4 2 4H5" }]],
  list: [["path", { d: "M8 6h12M8 12h12M8 18h12M4 6h.1M4 12h.1M4 18h.1" }]],
  // evidence kinds
  metric: [["path", { d: "M4 20V10M10 20V4M16 20v-7M21 20H3" }]],
  log: [
    ["path", { d: "M6 3h9l4 4v14H6z" }],
    ["path", { d: "M9 11h7M9 15h7M9 7h3" }],
  ],
  trace: [
    ["circle", { cx: 6, cy: 6, r: 2.2 }],
    ["circle", { cx: 18, cy: 12, r: 2.2 }],
    ["circle", { cx: 6, cy: 18, r: 2.2 }],
    ["path", { d: "M8.2 6h4a3 3 0 0 1 3 3v.8M8.2 18h4a3 3 0 0 0 3-3v-.8" }],
  ],
  change: [
    ["circle", { cx: 12, cy: 12, r: 3.2 }],
    ["path", { d: "M3 12h5.8M15.2 12H21" }],
  ],
  alert: [
    ["path", { d: "M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15z" }],
    ["path", { d: "M10 20.5h4" }],
  ],
  error_group: [
    ["rect", { x: 7, y: 7, width: 10, height: 13, rx: 5 }],
    ["path", { d: "M12 11v5M4 12h3M17 12h3M5 18l2.5-1.5M19 18l-2.5-1.5M9 4l1.5 2.5M15 4l-1.5 2.5" }],
  ],
  dependency: [
    ["ellipse", { cx: 12, cy: 5.5, rx: 7, ry: 2.5 }],
    ["path", { d: "M5 5.5v13c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5v-13M5 12c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5" }],
  ],
  runbook: [
    ["path", { d: "M3 5.5c3-1.5 6-1.5 9 .5 3-2 6-2 9-.5V19c-3-1.5-6-1.5-9 .5-3-2-6-2-9-.5z" }],
    ["path", { d: "M12 6v13.5" }],
  ],
  past_incident: [
    ["path", { d: "M3.5 12a8.5 8.5 0 1 0 2.5-6" }],
    ["path", { d: "M3 3.5V8h4.5M12 8v4.5l3 1.8" }],
  ],
  gap: [
    ["circle", { cx: 12, cy: 12, r: 9 }],
    ["path", { d: "M5.6 18.4 18.4 5.6" }],
  ],
};

/** An <svg> for one of the names above, hidden from assistive technology. */
export function icon(name, className = "icon") {
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  svg.setAttribute("class", className);
  for (const [tag, attrs] of SHAPES[name] || SHAPES.info) {
    const shape = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) shape.setAttribute(k, String(v));
    svg.append(shape);
  }
  return svg;
}

/**
 * A three-segment ring for the platform's categorical confidence (ADR-53: low, medium, high).
 * `level` is 0 (not yet), 1, 2 or 3 filled segments; `tone` is a class suffix for the colour.
 */
export function confidenceRing(level, tone) {
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 64 64");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  svg.setAttribute("class", `ring ring-${tone}`);
  // Three arcs of 110 degrees with 10-degree gaps, starting at the top.
  const r = 26;
  const arc = (startDeg, endDeg) => {
    const rad = (d) => ((d - 90) * Math.PI) / 180;
    const x1 = 32 + r * Math.cos(rad(startDeg));
    const y1 = 32 + r * Math.sin(rad(startDeg));
    const x2 = 32 + r * Math.cos(rad(endDeg));
    const y2 = 32 + r * Math.sin(rad(endDeg));
    return `M${x1.toFixed(2)} ${y1.toFixed(2)}A${r} ${r} 0 0 1 ${x2.toFixed(2)} ${y2.toFixed(2)}`;
  };
  for (let i = 0; i < 3; i += 1) {
    const p = document.createElementNS(NS, "path");
    p.setAttribute("d", arc(i * 120 + 5, i * 120 + 115));
    p.setAttribute("class", i < level ? "seg on" : "seg");
    svg.append(p);
  }
  return svg;
}
