/**
 * The historical ice control.
 *
 * 🔑 IT SELECTS MEASUREMENTS, NOT TIME. Every option is a date a satellite actually flew,
 * so there is no position on this control that shows you something nobody observed.
 *
 * ⚠️ IT WAS A SLIDER AND IS NOW A MONTH LIST, and the swap is worth understanding rather
 * than being read as a preference. A range input made the right guarantee for the wrong
 * reason: it was safe only because it was an INDEX into the vendored list, which nothing
 * on screen said. It looked like a continuous scrub across five years, so it invited being
 * dragged, and dragging it fired a rebuild of a six-thousand-polygon layer per step. A
 * list of months cannot be dragged, names every option, and makes "one measurement per
 * month, five years" legible instead of implied.
 *
 * ⚠️ CONCENTRATION IS NOT THICKNESS, and the caveat rides on the readout rather than
 * living in the README. It is the fraction of sea surface covered by ice on a 25 km grid.
 * A cell reading 90% says nothing about the particular hundred metres under a vehicle.
 */
import { useEffect, useMemo, useState } from "react";

import { useStore } from "./store";

/** Milliseconds per measurement while playing. Slow enough to read the month. */
const STEP_MS = 320;

const MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];

function monthOf(iso: string): string {
  return MONTHS[Number(iso.split("-")[1]) - 1];
}

function yearOf(iso: string): string {
  return iso.split("-")[0];
}

function label(iso: string): string {
  return `${monthOf(iso)} ${yearOf(iso)}`;
}

export function IceTimebar() {
  const ice = useStore((s) => s.ice);
  const setIceDate = useStore((s) => s.setIceDate);
  const scrubbing = useStore((s) => s.iceScrubbing);
  const setScrubbing = useStore((s) => s.setIceScrubbing);

  // 🔑 THE PICKER HOLDS A CHOICE, THE BUTTON APPLIES IT, and that split is the reason the
  // button exists. Rebuilding the ice layer costs a couple of hundred milliseconds of main
  // thread, so a select that applied on change paid it for every month you passed through:
  // once per keystroke when arrowing down the list, and once per option on a platform that
  // fires change while the list is still open. Choosing is now free and only committing
  // costs anything.
  const [pending, setPending] = useState<string | null>(null);

  // ⚠️ MEMOISED, AND NOT FOR TIDINESS. `ice?.dates ?? []` builds a NEW empty array on every
  // render whenever `ice` is absent, so it is a fresh reference each time. It is a
  // dependency of the playback effect below, which therefore tore down and re-armed its
  // timeout on every render: play mode would step at whatever rate React happened to
  // re-render rather than at STEP_MS. Found by react-hooks/exhaustive-deps.
  const dates = useMemo(() => ice?.dates ?? [], [ice]);
  // Indexed off the date actually SHOWN, not off the requested one. Those differ until
  // the first snap resolves, and driving the control from the request would let it sit
  // somewhere the map is not.
  const i = ice ? dates.indexOf(ice.date) : -1;

  // Play steps one measurement at a time and wraps. It is deliberately not tied to
  // anything else on screen: this is five years of history, and nothing else here has a
  // timeline at all.
  useEffect(() => {
    if (!scrubbing || i < 0 || dates.length === 0) return;
    const t = setTimeout(() => {
      setPending(null);
      setIceDate(dates[(i + 1) % dates.length]);
    }, STEP_MS);
    return () => clearTimeout(t);
  }, [scrubbing, i, dates, setIceDate]);

  if (!ice || i < 0) {
    return (
      <div className="timebar">
        <span className="dim">loading ice measurements…</span>
      </div>
    );
  }

  // ⚠️ The steppers and play apply immediately and CLEAR the pending choice. They are each
  // one deliberate action already, so making them wait behind GO would be ceremony; and
  // leaving a pending value behind would show the picker on one month while the map drew
  // another, which is the exact confusion the button is meant to remove.
  const step = (by: number) => {
    const n = Math.min(dates.length - 1, Math.max(0, i + by));
    setPending(null);
    setIceDate(dates[n]);
  };

  // Grouped by year, because 55 flat options is a scroll and "which year am I in" is the
  // first thing you want to know. The groups come from the data rather than from a range,
  // so a year with a missing month simply has fewer options under it and the control
  // still cannot offer a date nobody measured.
  const years: { year: string; dates: string[] }[] = [];
  for (const d of dates) {
    const y = yearOf(d);
    const last = years[years.length - 1];
    if (last && last.year === y) last.dates.push(d);
    else years.push({ year: y, dates: [d] });
  }

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

      <select
        className="icepick"
        value={pending ?? ice.date}
        onChange={(e) => setPending(e.target.value)}
        aria-label="Sea ice measurement month"
      >
        {years.map((y) => (
          <optgroup key={y.year} label={y.year}>
            {y.dates.map((d) => (
              <option key={d} value={d}>
                {label(d)}
              </option>
            ))}
          </optgroup>
        ))}
      </select>

      {/* Disabled when the choice already IS what is drawn, so the button says whether
          there is anything outstanding rather than being a control you press hopefully. */}
      <button
        className="icego"
        onClick={() => {
          if (pending) setIceDate(pending);
          setPending(null);
        }}
        disabled={!pending || pending === ice.date}
        title="Draw the selected measurement"
      >
        GO
      </button>

      <button
        className="stepbtn"
        onClick={() => step(1)}
        disabled={i === dates.length - 1}
        title="Next measurement"
      >
        ›
      </button>

      {/* 🔑 THE SELECT IS THE READOUT. There was a separate date label here and it printed
          the same month the picker already shows, two centimetres apart. One fact, one
          place. What earns its own text is the thing the picker cannot say: WHICH of the
          measurements you are on, so "one per month, five years" is legible rather than
          implied by a list you would have to scroll to count. */}
      <span className="icedate dim">
        measurement {i + 1} of {dates.length}
      </span>

      <span className="dim caveat" title={`${ice.caveat}\n\n${ice.citation}`}>
        measured sea ice concentration, not thickness
      </span>
    </div>
  );
}
