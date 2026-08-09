/**
 * The VIEW menu: which kinds are drawn.
 *
 * 🔑 A CLUTTER CONTROL, NOT AN HONESTY ONE, and it sits beside a control that is the other
 * thing so the difference is worth stating. HIDE UNDETECTED UNKNOWN is about what the
 * console may claim, it is checked by default, and its default is an argument. This is about
 * what one operator wants to look at right now, everything starts on, and turning a kind off
 * says nothing about the world.
 *
 * ⚠️ WHICH IS WHY THE STATUS STRIP KEEPS COUNTING THE WORLD rather than the view. Twelve
 * radar sites do not stop existing because somebody is trying to read a cluster underneath
 * them, and a count that moves with a display preference is a count you cannot quote.
 *
 * 🔑 SO THE FILTER ANNOUNCES ITSELF ON ITS OWN BUTTON instead, which reads `VIEW · 2 OFF`
 * whenever anything is switched off. The strip stays honest about the world, and nobody has
 * to wonder why the map looks emptier than the numbers under it. A filter with no visible
 * trace is how somebody spends ten minutes looking for an asset they themselves hid.
 */
import { useEffect, useRef } from "react";

import { KIND_LABEL, type AssetKind } from "./assets";
import { useStore } from "./store";

/**
 * The order the kinds are listed in, and it is deliberate rather than alphabetical.
 *
 * Own deployable assets first, because they are what the operator is responsible for. Then
 * the infrastructure they work alongside. Then contacts, which are what they are looking
 * FOR. Marker last: it is the only kind a person creates rather than one the world came
 * with.
 */
const ORDER: AssetKind[] = [
  "node",
  "uas",
  "patrol",
  "ground_party",
  "hydrophone",
  "launch_site",
  "radar",
  "vessel",
  "aircraft",
  "marker",
];

export function ViewMenu({ open, onOpenChange }: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  const hiddenKinds = useStore((s) => s.hiddenKinds);
  const toggleKind = useStore((s) => s.toggleKind);
  const wrap = useRef<HTMLDivElement>(null);

  // Closes on a click anywhere else and on Escape. A menu that can only be dismissed by
  // finding its own button again is the kind of thing that gets left open over the map.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      if (!wrap.current?.contains(e.target as Node)) onOpenChange(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onOpenChange(false);
    };
    window.addEventListener("pointerdown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("pointerdown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [open, onOpenChange]);

  return (
    <div className="viewmenu" ref={wrap}>
      <button
        onClick={() => onOpenChange(!open)}
        aria-expanded={open}
        aria-haspopup="true"
        title="which kinds are drawn on the map"
      >
        VIEW{hiddenKinds.length > 0 && ` · ${hiddenKinds.length} OFF`}
      </button>

      {open && (
        <div className="viewpanel" role="group" aria-label="which kinds are drawn">
          {ORDER.map((k) => (
            <label className="toggle" key={k}>
              <input
                type="checkbox"
                checked={!hiddenKinds.includes(k)}
                onChange={() => toggleKind(k)}
              />
              SHOW {KIND_LABEL[k].toUpperCase()}
            </label>
          ))}
        </div>
      )}
    </div>
  );
}
