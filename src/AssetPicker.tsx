/**
 * The pile, opened out. Icons float above where they actually are, each on a leader line
 * back to the spot they came from.
 *
 * 🔑 IT EXISTS BECAUSE THE MAP REFUSES TO HIDE ANYTHING. The icon layers set
 * `icon-allow-overlap: true` deliberately: an asset suppressed by collision detection is
 * worse than two overlapping, because the operator cannot tell "not there" from "not
 * drawn". Overlaps are therefore guaranteed by design, and a click on a pile has to
 * resolve somehow. Silently taking the topmost is the wrong answer, because "topmost" is
 * decided by draw order rather than by anything a person can see or predict.
 *
 * 🔒 THE LEADER LINE IS WHAT MAKES THIS HONEST, and it is not decoration. Every other
 * layer in this app refuses to draw anything at a position nobody measured: the ice will
 * not interpolate between measurements, the history trail is not animated between
 * samples. Fanning icons out breaks that rule unless the display says so, and the line is
 * how it says so. A floated icon is visibly tethered to its real position, so nobody can
 * read a bearing off the fan. Without the tether this would be a map quietly lying about
 * where things are.
 *
 * ⚠️ The fan is SCREEN geometry, so it dies the moment the camera moves. `GlobeMap`
 * clears it on `movestart` for exactly that reason: a tether pointing at a spot that has
 * since slid away is worse than no tether.
 */
import { useEffect } from "react";

import {
  ringState,
  weakAssetIds,
  KIND_LABEL,
  type Asset,
  type RingState,
} from "./assets";
import { iconMarkup } from "./map/icons";
import { useStore } from "./store";
import { useNow } from "./useNow";

const RING_LABEL: Record<Exclude<RingState, null>, string> = {
  unreachable: "UNREACHABLE",
  maintenance: "MAINTENANCE",
  weak: "WEAK LINK",
};

/** How far above the pile the fan sits, and how far apart its members are. */
const LIFT = 104;
const SPACING = 104;
const ICON = 30;
/** Keeps the fan clear of the top strip and the screen edges. */
const MARGIN = 16;
const TOP_STRIP = 52;

export function AssetPicker() {
  const picker = useStore((s) => s.picker);
  const assets = useStore((s) => s.assets);
  const mesh = useStore((s) => s.mesh);
  const select = useStore((s) => s.select);
  const setPicker = useStore((s) => s.setPicker);

  // Escape closes it. A thing that opens over the map and can only be dismissed by
  // clicking the map is a trap on a display where clicking the map does something else.
  useEffect(() => {
    if (!picker) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setPicker(null);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [picker, setPicker]);

  // ⚠️ ABOVE BOTH EARLY RETURNS. Hooks cannot run conditionally.
  const now = useNow();

  if (!picker) return null;

  const found = picker.ids
    .map((id) => assets.find((a) => a.id === id))
    .filter((a): a is Asset => a !== undefined);

  // The store refetches every few seconds, so a pile can lose members between the click
  // and the render. One survivor is not a choice, and none is not a fan.
  if (found.length < 2) return null;

  const weak = weakAssetIds(mesh);

  // Centred on the pile, then clamped so a cluster near an edge still opens fully on
  // screen. The tether is drawn to the true anchor either way, so clamping moves where
  // the icons sit without ever moving what they point at.
  const width = found.length * SPACING;
  const half = SPACING / 2;
  const minX = MARGIN + half;
  const maxX = window.innerWidth - MARGIN - width + half;
  const startX = Math.min(Math.max(picker.x - width / 2 + half, minX), Math.max(minX, maxX));
  const rowY = Math.max(TOP_STRIP + ICON, picker.y - LIFT);

  return (
    <>
      {/* Tethers, under the icons and ignoring the mouse entirely. */}
      <svg className="fanlines" aria-hidden="true">
        {found.map((a, n) => (
          <line
            key={a.id}
            x1={startX + n * SPACING}
            y1={rowY + ICON / 2}
            x2={picker.x}
            y2={picker.y}
          />
        ))}
        <circle cx={picker.x} cy={picker.y} r={3} />
      </svg>

      <div className="fan" role="listbox" aria-label={`${found.length} assets here, choose one`}>
        {found.map((a, n) => {
          const ring = ringState(a, weak, now);
          const dark = a.aisReporting === false;
          return (
            <button
              key={a.id}
              role="option"
              className={`fanitem${ring ? ` ${ring}` : ""}`}
              style={{ left: startX + n * SPACING, top: rowY }}
              onClick={() => {
                select(a.id);
                setPicker(null);
              }}
            >
              <span
                className="fanicon"
                // The same shape the map drew, from the same file, so the operator is
                // matching like against like rather than against an approximation.
                dangerouslySetInnerHTML={{
                  __html: iconMarkup(dark ? "vessel_dark" : a.kind, ICON),
                }}
              />
              <span className="fanname">{a.name}</span>
              <span className="fankind">
                {KIND_LABEL[a.kind] ?? a.kind}
                {ring && <span className={`fanflag ${ring}`}>{RING_LABEL[ring]}</span>}
              </span>
            </button>
          );
        })}
      </div>
    </>
  );
}
