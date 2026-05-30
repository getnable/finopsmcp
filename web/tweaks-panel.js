const __TWEAKS_STYLE = `
  .twk-panel{position:fixed;right:16px;bottom:16px;z-index:2147483646;width:280px;
    max-height:calc(100vh - 32px);display:flex;flex-direction:column;
    transform:scale(var(--dc-inv-zoom,1));transform-origin:bottom right;
    background:rgba(250,249,247,.78);color:#29261b;
    -webkit-backdrop-filter:blur(24px) saturate(160%);backdrop-filter:blur(24px) saturate(160%);
    border:.5px solid rgba(255,255,255,.6);border-radius:14px;
    box-shadow:0 1px 0 rgba(255,255,255,.5) inset,0 12px 40px rgba(0,0,0,.18);
    font:11.5px/1.4 ui-sans-serif,system-ui,-apple-system,sans-serif;overflow:hidden}
  .twk-hd{display:flex;align-items:center;justify-content:space-between;
    padding:10px 8px 10px 14px;cursor:move;user-select:none}
  .twk-hd b{font-size:12px;font-weight:600;letter-spacing:.01em}
  .twk-x{appearance:none;border:0;background:transparent;color:rgba(41,38,27,.55);
    width:22px;height:22px;border-radius:6px;cursor:default;font-size:13px;line-height:1}
  .twk-x:hover{background:rgba(0,0,0,.06);color:#29261b}
  .twk-body{padding:2px 14px 14px;display:flex;flex-direction:column;gap:10px;
    overflow-y:auto;overflow-x:hidden;min-height:0;
    scrollbar-width:thin;scrollbar-color:rgba(0,0,0,.15) transparent}
  .twk-body::-webkit-scrollbar{width:8px}
  .twk-body::-webkit-scrollbar-track{background:transparent;margin:2px}
  .twk-body::-webkit-scrollbar-thumb{background:rgba(0,0,0,.15);border-radius:4px;
    border:2px solid transparent;background-clip:content-box}
  .twk-body::-webkit-scrollbar-thumb:hover{background:rgba(0,0,0,.25);
    border:2px solid transparent;background-clip:content-box}
  .twk-row{display:flex;flex-direction:column;gap:5px}
  .twk-row-h{flex-direction:row;align-items:center;justify-content:space-between;gap:10px}
  .twk-lbl{display:flex;justify-content:space-between;align-items:baseline;
    color:rgba(41,38,27,.72)}
  .twk-lbl>span:first-child{font-weight:500}
  .twk-val{color:rgba(41,38,27,.5);font-variant-numeric:tabular-nums}

  .twk-sect{font-size:10px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;
    color:rgba(41,38,27,.45);padding:10px 0 0}
  .twk-sect:first-child{padding-top:0}

  .twk-field{appearance:none;box-sizing:border-box;width:100%;min-width:0;height:26px;padding:0 8px;
    border:.5px solid rgba(0,0,0,.1);border-radius:7px;
    background:rgba(255,255,255,.6);color:inherit;font:inherit;outline:none}
  .twk-field:focus{border-color:rgba(0,0,0,.25);background:rgba(255,255,255,.85)}
  select.twk-field{padding-right:22px;
    background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'><path fill='rgba(0,0,0,.5)' d='M0 0h10L5 6z'/></svg>");
    background-repeat:no-repeat;background-position:right 8px center}

  .twk-slider{appearance:none;-webkit-appearance:none;width:100%;height:4px;margin:6px 0;
    border-radius:999px;background:rgba(0,0,0,.12);outline:none}
  .twk-slider::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;
    width:14px;height:14px;border-radius:50%;background:#fff;
    border:.5px solid rgba(0,0,0,.12);box-shadow:0 1px 3px rgba(0,0,0,.2);cursor:default}
  .twk-slider::-moz-range-thumb{width:14px;height:14px;border-radius:50%;
    background:#fff;border:.5px solid rgba(0,0,0,.12);box-shadow:0 1px 3px rgba(0,0,0,.2);cursor:default}

  .twk-seg{position:relative;display:flex;padding:2px;border-radius:8px;
    background:rgba(0,0,0,.06);user-select:none}
  .twk-seg-thumb{position:absolute;top:2px;bottom:2px;border-radius:6px;
    background:rgba(255,255,255,.9);box-shadow:0 1px 2px rgba(0,0,0,.12);
    transition:left .15s cubic-bezier(.3,.7,.4,1),width .15s}
  .twk-seg.dragging .twk-seg-thumb{transition:none}
  .twk-seg button{appearance:none;position:relative;z-index:1;flex:1;border:0;
    background:transparent;color:inherit;font:inherit;font-weight:500;min-height:22px;
    border-radius:6px;cursor:default;padding:4px 6px;line-height:1.2;
    overflow-wrap:anywhere}

  .twk-toggle{position:relative;width:32px;height:18px;border:0;border-radius:999px;
    background:rgba(0,0,0,.15);transition:background .15s;cursor:default;padding:0}
  .twk-toggle[data-on="1"]{background:#34c759}
  .twk-toggle i{position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;
    background:#fff;box-shadow:0 1px 2px rgba(0,0,0,.25);transition:transform .15s}
  .twk-toggle[data-on="1"] i{transform:translateX(14px)}

  .twk-num{display:flex;align-items:center;box-sizing:border-box;min-width:0;height:26px;padding:0 0 0 8px;
    border:.5px solid rgba(0,0,0,.1);border-radius:7px;background:rgba(255,255,255,.6)}
  .twk-num-lbl{font-weight:500;color:rgba(41,38,27,.6);cursor:ew-resize;
    user-select:none;padding-right:8px}
  .twk-num input{flex:1;min-width:0;height:100%;border:0;background:transparent;
    font:inherit;font-variant-numeric:tabular-nums;text-align:right;padding:0 8px 0 0;
    outline:none;color:inherit;-moz-appearance:textfield}
  .twk-num input::-webkit-inner-spin-button,.twk-num input::-webkit-outer-spin-button{
    -webkit-appearance:none;margin:0}
  .twk-num-unit{padding-right:8px;color:rgba(41,38,27,.45)}

  .twk-btn{appearance:none;height:26px;padding:0 12px;border:0;border-radius:7px;
    background:rgba(0,0,0,.78);color:#fff;font:inherit;font-weight:500;cursor:default}
  .twk-btn:hover{background:rgba(0,0,0,.88)}
  .twk-btn.secondary{background:rgba(0,0,0,.06);color:inherit}
  .twk-btn.secondary:hover{background:rgba(0,0,0,.1)}

  .twk-swatch{appearance:none;-webkit-appearance:none;width:56px;height:22px;
    border:.5px solid rgba(0,0,0,.1);border-radius:6px;padding:0;cursor:default;
    background:transparent;flex-shrink:0}
  .twk-swatch::-webkit-color-swatch-wrapper{padding:0}
  .twk-swatch::-webkit-color-swatch{border:0;border-radius:5.5px}
  .twk-swatch::-moz-color-swatch{border:0;border-radius:5.5px}

  .twk-chips{display:flex;gap:6px}
  .twk-chip{position:relative;appearance:none;flex:1;min-width:0;height:46px;
    padding:0;border:0;border-radius:6px;overflow:hidden;cursor:default;
    box-shadow:0 0 0 .5px rgba(0,0,0,.12),0 1px 2px rgba(0,0,0,.06);
    transition:transform .12s cubic-bezier(.3,.7,.4,1),box-shadow .12s}
  .twk-chip:hover{transform:translateY(-1px);
    box-shadow:0 0 0 .5px rgba(0,0,0,.18),0 4px 10px rgba(0,0,0,.12)}
  .twk-chip[data-on="1"]{box-shadow:0 0 0 1.5px rgba(0,0,0,.85),
    0 2px 6px rgba(0,0,0,.15)}
  .twk-chip>span{position:absolute;top:0;bottom:0;right:0;width:34%;
    display:flex;flex-direction:column;box-shadow:-1px 0 0 rgba(0,0,0,.1)}
  .twk-chip>span>i{flex:1;box-shadow:0 -1px 0 rgba(0,0,0,.1)}
  .twk-chip>span>i:first-child{box-shadow:none}
  .twk-chip svg{position:absolute;top:6px;left:6px;width:13px;height:13px;
    filter:drop-shadow(0 1px 1px rgba(0,0,0,.3))}
`;
function useTweaks(defaults) {
  const [values, setValues] = React.useState(defaults);
  const setTweak = React.useCallback((keyOrEdits, val) => {
    const edits = typeof keyOrEdits === "object" && keyOrEdits !== null ? keyOrEdits : { [keyOrEdits]: val };
    setValues((prev) => ({ ...prev, ...edits }));
    window.parent.postMessage({ type: "__edit_mode_set_keys", edits }, "*");
    window.dispatchEvent(new CustomEvent("tweakchange", { detail: edits }));
  }, []);
  return [values, setTweak];
}
function TweaksPanel({ title = "Tweaks", children }) {
  const [open, setOpen] = React.useState(false);
  const dragRef = React.useRef(null);
  const offsetRef = React.useRef({ x: 16, y: 16 });
  const PAD = 16;
  const clampToViewport = React.useCallback(() => {
    const panel = dragRef.current;
    if (!panel) return;
    const w = panel.offsetWidth, h = panel.offsetHeight;
    const maxRight = Math.max(PAD, window.innerWidth - w - PAD);
    const maxBottom = Math.max(PAD, window.innerHeight - h - PAD);
    offsetRef.current = {
      x: Math.min(maxRight, Math.max(PAD, offsetRef.current.x)),
      y: Math.min(maxBottom, Math.max(PAD, offsetRef.current.y))
    };
    panel.style.right = offsetRef.current.x + "px";
    panel.style.bottom = offsetRef.current.y + "px";
  }, []);
  React.useEffect(() => {
    if (!open) return;
    clampToViewport();
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", clampToViewport);
      return () => window.removeEventListener("resize", clampToViewport);
    }
    const ro = new ResizeObserver(clampToViewport);
    ro.observe(document.documentElement);
    return () => ro.disconnect();
  }, [open, clampToViewport]);
  React.useEffect(() => {
    const onMsg = (e) => {
      const t = e?.data?.type;
      if (t === "__activate_edit_mode") setOpen(true);
      else if (t === "__deactivate_edit_mode") setOpen(false);
    };
    window.addEventListener("message", onMsg);
    window.parent.postMessage({ type: "__edit_mode_available" }, "*");
    return () => window.removeEventListener("message", onMsg);
  }, []);
  const dismiss = () => {
    setOpen(false);
    window.parent.postMessage({ type: "__edit_mode_dismissed" }, "*");
  };
  const onDragStart = (e) => {
    const panel = dragRef.current;
    if (!panel) return;
    const r = panel.getBoundingClientRect();
    const sx = e.clientX, sy = e.clientY;
    const startRight = window.innerWidth - r.right;
    const startBottom = window.innerHeight - r.bottom;
    const move = (ev) => {
      offsetRef.current = {
        x: startRight - (ev.clientX - sx),
        y: startBottom - (ev.clientY - sy)
      };
      clampToViewport();
    };
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  };
  if (!open) return null;
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("style", null, __TWEAKS_STYLE), /* @__PURE__ */ React.createElement(
    "div",
    {
      ref: dragRef,
      className: "twk-panel",
      "data-omelette-chrome": "",
      style: { right: offsetRef.current.x, bottom: offsetRef.current.y }
    },
    /* @__PURE__ */ React.createElement("div", { className: "twk-hd", onMouseDown: onDragStart }, /* @__PURE__ */ React.createElement("b", null, title), /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "twk-x",
        "aria-label": "Close tweaks",
        onMouseDown: (e) => e.stopPropagation(),
        onClick: dismiss
      },
      "x"
    )),
    /* @__PURE__ */ React.createElement("div", { className: "twk-body" }, children)
  ));
}
function TweakSection({ label, children }) {
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { className: "twk-sect" }, label), children);
}
function TweakRow({ label, value, children, inline = false }) {
  return /* @__PURE__ */ React.createElement("div", { className: inline ? "twk-row twk-row-h" : "twk-row" }, /* @__PURE__ */ React.createElement("div", { className: "twk-lbl" }, /* @__PURE__ */ React.createElement("span", null, label), value != null && /* @__PURE__ */ React.createElement("span", { className: "twk-val" }, value)), children);
}
function TweakSlider({ label, value, min = 0, max = 100, step = 1, unit = "", onChange }) {
  return /* @__PURE__ */ React.createElement(TweakRow, { label, value: `${value}${unit}` }, /* @__PURE__ */ React.createElement(
    "input",
    {
      type: "range",
      className: "twk-slider",
      min,
      max,
      step,
      value,
      onChange: (e) => onChange(Number(e.target.value))
    }
  ));
}
function TweakToggle({ label, value, onChange }) {
  return /* @__PURE__ */ React.createElement("div", { className: "twk-row twk-row-h" }, /* @__PURE__ */ React.createElement("div", { className: "twk-lbl" }, /* @__PURE__ */ React.createElement("span", null, label)), /* @__PURE__ */ React.createElement(
    "button",
    {
      type: "button",
      className: "twk-toggle",
      "data-on": value ? "1" : "0",
      role: "switch",
      "aria-checked": !!value,
      onClick: () => onChange(!value)
    },
    /* @__PURE__ */ React.createElement("i", null)
  ));
}
function TweakRadio({ label, value, options, onChange }) {
  const trackRef = React.useRef(null);
  const [dragging, setDragging] = React.useState(false);
  const valueRef = React.useRef(value);
  valueRef.current = value;
  const labelLen = (o) => String(typeof o === "object" ? o.label : o).length;
  const maxLen = options.reduce((m, o) => Math.max(m, labelLen(o)), 0);
  const fitsAsSegments = maxLen <= ({ 2: 16, 3: 10 }[options.length] ?? 0);
  if (!fitsAsSegments) {
    const resolve = (s) => {
      const m = options.find((o) => String(typeof o === "object" ? o.value : o) === s);
      return m === void 0 ? s : typeof m === "object" ? m.value : m;
    };
    return /* @__PURE__ */ React.createElement(
      TweakSelect,
      {
        label,
        value,
        options,
        onChange: (s) => onChange(resolve(s))
      }
    );
  }
  const opts = options.map((o) => typeof o === "object" ? o : { value: o, label: o });
  const idx = Math.max(0, opts.findIndex((o) => o.value === value));
  const n = opts.length;
  const segAt = (clientX) => {
    const r = trackRef.current.getBoundingClientRect();
    const inner = r.width - 4;
    const i = Math.floor((clientX - r.left - 2) / inner * n);
    return opts[Math.max(0, Math.min(n - 1, i))].value;
  };
  const onPointerDown = (e) => {
    setDragging(true);
    const v0 = segAt(e.clientX);
    if (v0 !== valueRef.current) onChange(v0);
    const move = (ev) => {
      if (!trackRef.current) return;
      const v = segAt(ev.clientX);
      if (v !== valueRef.current) onChange(v);
    };
    const up = () => {
      setDragging(false);
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };
  return /* @__PURE__ */ React.createElement(TweakRow, { label }, /* @__PURE__ */ React.createElement(
    "div",
    {
      ref: trackRef,
      role: "radiogroup",
      onPointerDown,
      className: dragging ? "twk-seg dragging" : "twk-seg"
    },
    /* @__PURE__ */ React.createElement(
      "div",
      {
        className: "twk-seg-thumb",
        style: {
          left: `calc(2px + ${idx} * (100% - 4px) / ${n})`,
          width: `calc((100% - 4px) / ${n})`
        }
      }
    ),
    opts.map((o) => /* @__PURE__ */ React.createElement("button", { key: o.value, type: "button", role: "radio", "aria-checked": o.value === value }, o.label))
  ));
}
function TweakSelect({ label, value, options, onChange }) {
  return /* @__PURE__ */ React.createElement(TweakRow, { label }, /* @__PURE__ */ React.createElement("select", { className: "twk-field", value, onChange: (e) => onChange(e.target.value) }, options.map((o) => {
    const v = typeof o === "object" ? o.value : o;
    const l = typeof o === "object" ? o.label : o;
    return /* @__PURE__ */ React.createElement("option", { key: v, value: v }, l);
  })));
}
function TweakText({ label, value, placeholder, onChange }) {
  return /* @__PURE__ */ React.createElement(TweakRow, { label }, /* @__PURE__ */ React.createElement(
    "input",
    {
      className: "twk-field",
      type: "text",
      value,
      placeholder,
      onChange: (e) => onChange(e.target.value)
    }
  ));
}
function TweakNumber({ label, value, min, max, step = 1, unit = "", onChange }) {
  const clamp = (n) => {
    if (min != null && n < min) return min;
    if (max != null && n > max) return max;
    return n;
  };
  const startRef = React.useRef({ x: 0, val: 0 });
  const onScrubStart = (e) => {
    e.preventDefault();
    startRef.current = { x: e.clientX, val: value };
    const decimals = (String(step).split(".")[1] || "").length;
    const move = (ev) => {
      const dx = ev.clientX - startRef.current.x;
      const raw = startRef.current.val + dx * step;
      const snapped = Math.round(raw / step) * step;
      onChange(clamp(Number(snapped.toFixed(decimals))));
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };
  return /* @__PURE__ */ React.createElement("div", { className: "twk-num" }, /* @__PURE__ */ React.createElement("span", { className: "twk-num-lbl", onPointerDown: onScrubStart }, label), /* @__PURE__ */ React.createElement(
    "input",
    {
      type: "number",
      value,
      min,
      max,
      step,
      onChange: (e) => onChange(clamp(Number(e.target.value)))
    }
  ), unit && /* @__PURE__ */ React.createElement("span", { className: "twk-num-unit" }, unit));
}
function __twkIsLight(hex) {
  const h = String(hex).replace("#", "");
  const x = h.length === 3 ? h.replace(/./g, (c) => c + c) : h.padEnd(6, "0");
  const n = parseInt(x.slice(0, 6), 16);
  if (Number.isNaN(n)) return true;
  const r = n >> 16 & 255, g = n >> 8 & 255, b = n & 255;
  return r * 299 + g * 587 + b * 114 > 148e3;
}
const __TwkCheck = ({ light }) => /* @__PURE__ */ React.createElement("svg", { viewBox: "0 0 14 14", "aria-hidden": "true" }, /* @__PURE__ */ React.createElement(
  "path",
  {
    d: "M3 7.2 5.8 10 11 4.2",
    fill: "none",
    strokeWidth: "2.2",
    strokeLinecap: "round",
    strokeLinejoin: "round",
    stroke: light ? "rgba(0,0,0,.78)" : "#fff"
  }
));
function TweakColor({ label, value, options, onChange }) {
  if (!options || !options.length) {
    return /* @__PURE__ */ React.createElement("div", { className: "twk-row twk-row-h" }, /* @__PURE__ */ React.createElement("div", { className: "twk-lbl" }, /* @__PURE__ */ React.createElement("span", null, label)), /* @__PURE__ */ React.createElement(
      "input",
      {
        type: "color",
        className: "twk-swatch",
        value,
        onChange: (e) => onChange(e.target.value)
      }
    ));
  }
  const key = (o) => String(JSON.stringify(o)).toLowerCase();
  const cur = key(value);
  return /* @__PURE__ */ React.createElement(TweakRow, { label }, /* @__PURE__ */ React.createElement("div", { className: "twk-chips", role: "radiogroup" }, options.map((o, i) => {
    const colors = Array.isArray(o) ? o : [o];
    const [hero, ...rest] = colors;
    const sup = rest.slice(0, 4);
    const on = key(o) === cur;
    return /* @__PURE__ */ React.createElement(
      "button",
      {
        key: i,
        type: "button",
        className: "twk-chip",
        role: "radio",
        "aria-checked": on,
        "data-on": on ? "1" : "0",
        "aria-label": colors.join(", "),
        title: colors.join(" \xB7 "),
        style: { background: hero },
        onClick: () => onChange(o)
      },
      sup.length > 0 && /* @__PURE__ */ React.createElement("span", null, sup.map((c, j) => /* @__PURE__ */ React.createElement("i", { key: j, style: { background: c } }))),
      on && /* @__PURE__ */ React.createElement(__TwkCheck, { light: __twkIsLight(hero) })
    );
  })));
}
function TweakButton({ label, onClick, secondary = false }) {
  return /* @__PURE__ */ React.createElement(
    "button",
    {
      type: "button",
      className: secondary ? "twk-btn secondary" : "twk-btn",
      onClick
    },
    label
  );
}
Object.assign(window, {
  useTweaks,
  TweaksPanel,
  TweakSection,
  TweakRow,
  TweakSlider,
  TweakToggle,
  TweakRadio,
  TweakSelect,
  TweakText,
  TweakNumber,
  TweakColor,
  TweakButton
});
