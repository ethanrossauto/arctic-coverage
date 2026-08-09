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
import { useMemo, useState, type ReactNode } from "react";

import { useStore } from "./store";

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

/**
 * `children` are placed at the RIGHT END of this row, and the slot exists so that the
 * status strip below can hold nothing but counts.
 *
 * 🔑 A SLOT RATHER THAN A SECOND POSITIONED ELEMENT. This row is absolutely positioned at
 * a fixed offset above the footer, and the one previous attempt in this file to keep two
 * things at the same offset by giving both the same `bottom` is recorded three comments
 * up as having silently overlapped when one of them grew. Passing the content in means
 * there is one row, laid out by one flex container, and it cannot come apart.
 */
export function IceTimebar({ children }: { children?: ReactNode }) {
  const ice = useStore((s) => s.ice);
  const setIceDate = useStore((s) => s.setIceDate);

  // 🔑 THE PICKER HOLDS A CHOICE, THE BUTTON APPLIES IT, and that split is the reason the
  // button exists. Changing month fetches that month's measurement tile, so a select that
  // applied on change paid for every month you passed through: once per keystroke when
  // arrowing down the list, and once per option on a platform that fires change while the
  // list is still open. Choosing is now free and only committing costs anything.
  const [chosen, setChosen] = useState<string | null>(null);

  // ⚠️ MEMOISED BECAUSE `ice?.dates ?? []` BUILDS A NEW EMPTY ARRAY EVERY RENDER while the
  // measurements are still loading, and a fresh reference each time is a dependency that
  // never settles. The specific bug it was written for is gone with the playback effect
  // that used to read it; the reference is still worth keeping stable and the reason is no
  // longer that one.
  const dates = useMemo(() => ice?.dates ?? [], [ice]);
  // Indexed off the date actually SHOWN, not off the requested one. Those differ until
  // the first snap resolves, and driving the control from the request would let it sit
  // somewhere the map is not.
  const i = ice ? dates.indexOf(ice.date) : -1;

  if (!ice || i < 0) {
    return (
      <div className="timebar">
        <span className="dim">loading ice measurements…</span>
        {/* Rendered on the loading path too. The disclosure and the world controls have
            nothing to do with ice, so having them appear only once a tile has downloaded
            would blank the reset button for the first second of every visit. */}
        {children && <span className="timebar-right">{children}</span>}
      </div>
    );
  }

  // 🔑 DERIVED, NOT SYNCED. A choice only counts while it differs from what is drawn, so
  // "is something waiting" is a question about the current render rather than a second
  // piece of state to keep in step. An effect that watched the drawn date and cleared the
  // choice did the same job and was the last lint error in the tree: calling setState from
  // an effect to mirror a value you can compute is a cascading render for no reason.
  //
  // It also gives the behaviour the button needs for free. The picker keeps showing what
  // you asked for while the tile loads, instead of snapping back to the old month and
  // reading as though GO had failed.
  const pending = chosen && chosen !== ice.date ? chosen : null;


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
      {/* 🔑 A MONTH AND A GO, AND NOTHING ELSE. This carried a play button and a stepper
          either side of the picker, which is four ways to change one value. Stepping
          through sixty measurements is a thing to watch rather than a thing to read, and
          the select already reaches any month in one action. */}
      <span className="dim caveat" title={`${ice.caveat}\n\n${ice.citation}`}>
        measured sea ice concentration date
      </span>

      <select
        className="icepick"
        value={pending ?? ice.date}
        onChange={(e) => setChosen(e.target.value)}
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
        onClick={() => pending && setIceDate(pending)}
        disabled={!pending || pending === ice.date}
        title="Draw the selected measurement"
      >
        GO
      </button>

      {children && <span className="timebar-right">{children}</span>}
    </div>
  );
}
