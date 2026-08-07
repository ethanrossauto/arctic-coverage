/**
 * The historical ice timebar.
 *
 * 🔑 IT SCRUBS MEASUREMENTS, NOT TIME. The control is an index into the vendored list
 * of dates, so every position on it is a date a satellite actually flew. That is why it
 * is a stepped range rather than a date picker: a date picker invites you to type a day
 * nobody measured and then quietly shows you a different one.
 *
 * ⚠️ CONCENTRATION IS NOT THICKNESS, and the caveat rides on the readout rather than
 * living in the README. It is the fraction of sea surface covered by ice on a 25 km grid.
 * A cell reading 90% says nothing about the particular hundred metres under a vehicle.
 */
import { useEffect } from "react";

import { useStore } from "./store";

/** Milliseconds per measurement while playing. Slow enough to read the month. */
const STEP_MS = 320;

const MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];

function label(iso: string): string {
  const [y, m] = iso.split("-");
  return `${MONTHS[Number(m) - 1]} ${y}`;
}

export function IceTimebar() {
  const ice = useStore((s) => s.ice);
  const setIceDate = useStore((s) => s.setIceDate);
  const scrubbing = useStore((s) => s.iceScrubbing);
  const setScrubbing = useStore((s) => s.setIceScrubbing);

  const dates = ice?.dates ?? [];
  // Indexed off the date actually SHOWN, not off the requested one. Those differ until
  // the first snap resolves, and driving the slider from the request would let the handle
  // sit somewhere the map is not.
  const i = ice ? dates.indexOf(ice.date) : -1;

  // Play steps one measurement at a time and wraps. It is deliberately not tied to the
  // scenario clock: this is five years of history, that is minutes of one afternoon.
  useEffect(() => {
    if (!scrubbing || i < 0 || dates.length === 0) return;
    const t = setTimeout(() => setIceDate(dates[(i + 1) % dates.length]), STEP_MS);
    return () => clearTimeout(t);
  }, [scrubbing, i, dates, setIceDate]);

  if (!ice || i < 0) {
    return (
      <div className="timebar">
        <span className="dim">loading ice measurements…</span>
      </div>
    );
  }

  const step = (by: number) => {
    const n = Math.min(dates.length - 1, Math.max(0, i + by));
    setIceDate(dates[n]);
  };

  // A tick wherever the year rolls over, so five years read as five spans rather than
  // as fifty-five anonymous notches.
  const ticks = dates
    .map((d, idx) => ({ year: d.slice(0, 4), idx }))
    .filter((t, idx, all) => idx === 0 || all[idx - 1].year !== t.year);

  return (
    <div className="timebar">
      <button
        className="play"
        onClick={() => setScrubbing(!scrubbing)}
        title="Step through every measurement, oldest to newest"
      >
        {scrubbing ? "❚❚" : "▶"}
      </button>

      <button className="stepbtn" onClick={() => step(-1)} disabled={i === 0} title="Previous measurement">
        ‹
      </button>

      <div className="scrub">
        <input
          type="range"
          min={0}
          max={dates.length - 1}
          step={1}
          value={i}
          onChange={(e) => setIceDate(dates[Number(e.target.value)])}
          aria-label="Sea ice measurement date"
        />
        <div className="ticks">
          {ticks.map((t) => (
            <span key={t.year} style={{ left: `${(t.idx / (dates.length - 1)) * 100}%` }}>
              {t.year}
            </span>
          ))}
        </div>
      </div>

      <button
        className="stepbtn"
        onClick={() => step(1)}
        disabled={i === dates.length - 1}
        title="Next measurement"
      >
        ›
      </button>

      <span className="icedate">{label(ice.date)}</span>

      <span className="dim caveat" title={`${ice.caveat}\n\n${ice.citation}`}>
        measured sea ice concentration, not thickness
      </span>
    </div>
  );
}
