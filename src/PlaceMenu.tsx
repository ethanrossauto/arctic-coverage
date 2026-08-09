/**
 * The PLACE menu: put a new asset on the map by hand.
 *
 * 🔑 WHY IT EXISTS. Placing was reachable by command only, which made it the one action
 * that changes the world with no manual route to it. Anything the console can be told to do
 * it should also be possible to do, and a demo that can only place by speaking is a demo
 * that cannot place at all when the microphone is refused.
 *
 * 🔑 ARM, THEN CLICK. A menu can supply the kind and the flags; it cannot supply a
 * position, and asking for coordinates in a text field would be worse than the sentence it
 * replaced. So choosing arms the map, the next click on the globe places, and Escape or a
 * second press disarms. The map's own cursor and banner say which mode it is in, because an
 * armed map that looks identical to an idle one turns the next click into a surprise.
 *
 * ⚠️ IT SENDS A PLAN, NOT AN API CALL. The command bar owns the transcript, the busy state
 * and the audit trail, so a placement made by hand travels the same road as a spoken one
 * and shows up in the log the same way. `source: "ui_button"` is what tells them apart
 * afterwards, which is the distinction worth keeping rather than hiding.
 */
import { useEffect, useRef } from "react";

import { KIND_LABEL, type AssetKind } from "./assets";
import { useStore } from "./store";

/**
 * What the menu offers, and it is deliberately not every placeable kind.
 *
 * ⚠️ THE SERVER ACCEPTS TEN, INCLUDING `radar`. That one is left out here on purpose: a
 * radar site is third-party infrastructure this console works alongside, so an operator
 * conjuring one is claiming something about somebody else's network. The command path still
 * allows it, because a typed request is explicit in a way a menu item is not.
 */
const PLACEABLE: AssetKind[] = [
  "node",
  "hydrophone",
  "uas",
  "patrol",
  "launch_site",
  "vessel",
  "aircraft",
  "ground_party",
  "marker",
];

export function PlaceMenu({ open, onOpenChange }: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  const placing = useStore((s) => s.placing);
  const setPlacing = useStore((s) => s.setPlacing);
  const wrap = useRef<HTMLDivElement>(null);

  // Same dismissal rules as the VIEW menu beside it: click away or Escape. Escape also
  // disarms, because the mode is the thing an operator most wants to get out of quickly.
  useEffect(() => {
    if (!open && !placing.kind) return;
    const onDown = (e: PointerEvent) => {
      // ⚠️ ONLY THE MENU CLOSES ON AN OUTSIDE CLICK, NEVER THE ARMED MODE. The whole point
      // of arming is that the next click lands on the map, and dismissing on any outside
      // pointerdown would disarm on exactly that click and place nothing.
      if (!wrap.current?.contains(e.target as Node)) onOpenChange(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      onOpenChange(false);
      setPlacing({ ...placing, kind: null });
    };
    window.addEventListener("pointerdown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("pointerdown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [open, placing, onOpenChange, setPlacing]);

  const arm = (kind: AssetKind) => {
    // Pressing the armed kind again disarms, so the control is its own off switch and an
    // operator never has to find Escape to change their mind. The attributes survive both a
    // change of kind and disarming, so ticking a box and then reconsidering what to place
    // does not cost the tick.
    setPlacing({ ...placing, kind: placing.kind === kind ? null : kind });
  };

  return (
    <div className="viewmenu placemenu" ref={wrap}>
      <button
        onClick={() => onOpenChange(!open)}
        aria-expanded={open}
        aria-haspopup="true"
        className={placing.kind ? "armed" : undefined}
        title="put a new asset on the map: choose a kind, then click where it goes"
      >
        PLACE{placing.kind && ` · ${KIND_LABEL[placing.kind].toUpperCase()}`}
      </button>

      {open && (
        <div className="viewpanel" role="group" aria-label="place an asset">
          {PLACEABLE.map((k) => (
            <label className="toggle" key={k}>
              <input
                type="radio"
                name="placekind"
                checked={placing.kind === k}
                onChange={() => arm(k)}
              />
              {KIND_LABEL[k].toUpperCase()}
            </label>
          ))}

          {/* 🔑 THE ONE EDITABLE ATTRIBUTE, AND IT IS THE ONE THAT CHANGES BEHAVIOUR RATHER
              THAN THE LABEL. An unknown asset is classified unknown AND stops announcing
              itself, so whether it appears at all becomes a question about sensor coverage.
              Everything else about a placed asset is still fixed; see the README's future
              work note for what is queued to become editable.

              ⚠️ OFFERED ON EVERY KIND, INCLUDING THE ONES WHERE IT READS ODDLY. It was
              restricted to the three contact kinds on the argument that a mast we bolted
              down cannot be unidentified, which is true of the fiction and a nuisance in
              the control: half the list came up with the box greyed out and nothing on
              screen saying why. A menu that silently refuses is worse than one that lets
              you place something strange, and the strange case is recoverable. Narrowing
              it again is a later decision, made with a reason better than tidiness. */}
          <div className="placeattr">
            <label
              className="toggle"
              title="classify it unknown and silence it, so it is only visible if a sensor holds it"
            >
              <input
                type="checkbox"
                checked={placing.unknown}
                onChange={(e) => setPlacing({ ...placing, unknown: e.target.checked })}
              />
              UNKNOWN
            </label>

            {/* 🔑 THE OTHER THING THAT CHANGES BEHAVIOUR RATHER THAN THE LABEL, and the
                two together are why this section exists at all. A terminal is a way out of
                the theatre, so an asset carrying one is reachable on its own wherever it
                is put. Without one it is reachable only through the mesh, by a chain of
                assets we are hearing from that ends at something which does have one, so
                dropping a node on an empty stretch of coast and finding it cut off is the
                model working rather than a fault. */}
            <label
              className="toggle"
              title="give it its own satellite terminal. Without one it can only reach us through a neighbour that already can"
            >
              <input
                type="checkbox"
                checked={placing.backhaul}
                onChange={(e) => setPlacing({ ...placing, backhaul: e.target.checked })}
              />
              BACKHAUL
            </label>
          </div>

          <div className="placehint">
            {placing.kind ? "click the map to place it" : "choose a kind"}
          </div>
        </div>
      )}
    </div>
  );
}
