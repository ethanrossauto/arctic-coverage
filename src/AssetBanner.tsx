import { useEffect, useState } from "react";

import { runCommand } from "./commandRunner";
/**
 * What you get when you click an asset.
 *
 * 🔑 THE ROWS ARE A TABLE, NOT A RUN OF JSX, and that is what lets this stay honest as the
 * seed grows. Each row names a label and how to get its value; a row whose value is absent
 * is not rendered at all. So a kind that carries a field shows it, a kind that does not
 * simply has a shorter banner, and a new field is one line here rather than a new branch.
 *
 * 🔒 IT NEVER RENDERS "NOT AVAILABLE". A row that says nothing teaches an operator to skim
 * past rows, which costs more than the row was worth. Absent beats empty.
 *
 * ⚠️ ORDER IS BY WHAT YOU ACT ON, not by what the database happens to hold. Condition and
 * contact first, because those are the two questions this console exists for; physical
 * detail after; position last, because the map is already showing you where it is.
 */
import {
  connectionCount,
  isGateway,
  minutesSinceHeard,
  positionOf,
  ringState,
  speedOf,
  unknownState,
  weakAssetIds,
  KIND_LABEL,
  type Asset,
  type RingState,
} from "./assets";
import { useStore } from "./store";
import { useNow } from "./useNow";

/** How the ring states read in prose. Same three words the rings mean, spelled out. */
const RING_LABEL: Record<Exclude<RingState, null>, string> = {
  unreachable: "UNREACHABLE",
  maintenance: "MAINTENANCE",
  weak: "WEAK LINK",
};

/** Minutes into something a person would say out loud. */
function ago(minutes: number): string {
  const m = Math.round(minutes);
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m ago`;
  return `${Math.floor(h / 24)}d ${h % 24}h ago`;
}

function duration(minutes: number): string {
  const m = Math.round(minutes);
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

interface Row {
  label: string;
  /** The value, or null to leave the row out entirely. */
  value: string | number | null;
  /** Hover text, where what the number means is not obvious from the label. */
  title?: string;
  tone?: "warn" | "alert";
}

/** One field as the server declares it: the shape of an asset, owned server-side. */
interface FieldSpec {
  name: string;
  label: string;
  applies: string[];
  note: string;
  /** Free entry is bounded by this; `number` gets a numeric box. */
  type: string;
  /** Origin decides this, on the server. The client never guesses which fields may change. */
  editable: boolean;
  /** A closed set means a dropdown. Empty means free entry. */
  choices: string[];
  unit: string;
}

/**
 * Fetched once per page. The declaration cannot change under a running client, and the
 * panel opens often enough that refetching it per selection would be a request per click
 * for an answer that never moves.
 */
let cachedSchema: FieldSpec[] | null = null;

function useFieldSchema(): FieldSpec[] | null {
  const [schema, setSchema] = useState<FieldSpec[] | null>(cachedSchema);
  useEffect(() => {
    if (cachedSchema) return;
    let live = true;
    void fetch("/api/schema")
      .then((r) => (r.ok ? r.json() : null))
      .then((body) => {
        const f = body?.fields as FieldSpec[] | undefined;
        if (!f || !live) return;
        cachedSchema = f;
        setSchema(f);
      })
      // No schema, no rows. The panel header still names the asset, which is more useful
      // than an error about a declaration the operator has never heard of.
      .catch(() => {});
    return () => {
      live = false;
    };
  }, []);
  return schema;
}

/**
 * The one editing control, and it is a dropdown wherever the field is a closed set.
 *
 * 🔑 WHY EDITING IS MOUSE-ONLY. Most of what is editable here is a CHOICE: a payload is one
 * of the sensors that exist, a backhaul is on or off. Picking from a list is what a dropdown
 * is for and what speech is worst at, and a near-miss by voice does not fail loudly, it sets
 * a field to a string nothing answers to.
 *
 * ⚠️ IT POSTS A PLAN, NOT A SENTENCE, so it never reaches the model and costs no schema.
 * Same path the place button uses, and the executor validates it the same way.
 */
function FieldEditor({
  spec,
  asset,
  onDone,
}: {
  spec: FieldSpec;
  asset: Asset;
  onDone: () => void;
}) {
  const current = asset.props?.[spec.name];
  const [draft, setDraft] = useState(current === undefined || current === null ? "" : String(current));

  const commit = (value: string) => {
    if (value !== "" && value !== String(current ?? "")) {
      runCommand(`set ${spec.label} on ${asset.name} to ${value}`, "ui_button", {
        plan: [{ tool: "edit_asset", params: { target: asset.id, field: spec.name, value } }],
      });
    }
    onDone();
  };

  if (spec.choices.length > 0) {
    return (
      <select
        className="bedit-input"
        autoFocus
        value={draft}
        onChange={(e) => commit(e.target.value)}
        onBlur={onDone}
      >
        <option value="">choose…</option>
        {spec.choices.map((c) => (
          <option key={c} value={c}>
            {c}
          </option>
        ))}
      </select>
    );
  }

  return (
    <input
      className="bedit-input"
      autoFocus
      type={spec.type === "number" ? "number" : "text"}
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => commit(draft)}
      onKeyDown={(e) => {
        if (e.key === "Enter") commit(draft);
        // Escape abandons rather than committing, which is what every other editable field
        // anybody has used does.
        if (e.key === "Escape") onDone();
      }}
    />
  );
}

export function AssetBanner() {
  const selectedId = useStore((s) => s.selectedId);
  const assets = useStore((s) => s.assets);
  const mesh = useStore((s) => s.mesh);
  const select = useStore((s) => s.select);
  // ⚠️ ABOVE THE EARLY RETURN, because hooks cannot run conditionally. Reading the clock
  // used to happen after it, which was legal only because `Date.now()` is not a hook.
  const now = useNow();
  // ⚠️ AND SO IS THIS ONE, for the reason the comment above gives. It was below the early
  // return, which eslint caught: a hook after a conditional return runs in a different
  // order on the render where nothing is selected.
  const schema = useFieldSchema();
  const [editing, setEditing] = useState<string | null>(null);

  const asset: Asset | undefined = assets.find((a) => a.id === selectedId);
  if (!asset) return null;

  const ring = ringState(asset, weakAssetIds(mesh), now);
  const heard = minutesSinceHeard(asset, now);
  const connections = connectionCount(asset.id, mesh);
  const p = asset.props ?? {};

  // Plain English for the wire value. "third_party" is a schema word, not something to put
  // in front of an operator.
  // ⚠️ `contact` MAPPED TO null AND THAT WAS THE SAME BUG AS `detects to`: the server had an
  // answer and the panel said N/A. "Not us" is what the data actually supports for a contact,
  // since nothing here records who operates a ship we merely observe.
  const relationshipLabel = (r: string | undefined): string | null =>
    r === "ours"
      ? "us"
      : r === "third_party"
        ? "another operator"
        : r === "contact"
          ? "not us"
          : null;

  const str = (v: unknown): string | null => (typeof v === "string" && v ? v : null);
  const num = (v: unknown): number | null => (typeof v === "number" ? v : null);

  // Who is holding this contact. `first_detected_by` was a second name for the same fact
  // and went with the props trim.
  const heldBy = str(p.held_by);
  const altitude = asset.altM;

  // 🔑 KEYED BY THE FIELD NAME THE SERVER DECLARES, NOT BY A LABEL WRITTEN HERE. The order
  // and the wording now come from `/api/schema`; this map only knows how to turn one asset
  // into one value. That split is deliberate: the shape of an asset is a domain fact and
  // belongs on the server, while "how do I render a mesh count" is a rendering fact and
  // belongs here. `test_fields.py` pins the two lists together so neither can grow a member
  // the other has not heard of.
  const values: Record<string, { value: Row["value"]; tone?: Row["tone"] }> = {
    relationship: { value: relationshipLabel(asset.relationship) },
    threat: {
      value: str(asset.threat),
      tone: asset.threat === "hostile" ? "alert" : asset.threat === "unknown" ? "warn" : undefined,
    },
    classification: { value: str(p.classification) },
    last_heard: {
      value: heard === null ? null : ago(heard),
      tone: ring === "unreachable" ? "warn" : undefined,
    },
    connections: {
      value: connections === 0 ? "none" : connections,
      tone: connections === 0 ? "warn" : undefined,
    },
    mesh_connected: { value: asset.meshConnected === false ? "nothing" : null, tone: "warn" },
    avg_gateway_minutes: {
      value: num(p.avg_gateway_minutes) === null ? null : duration(num(p.avg_gateway_minutes)!),
    },
    ais_reporting: {
      value: asset.aisReporting === null ? null : asset.aisReporting ? "yes, AIS" : "NO AIS",
      tone: asset.aisReporting === false ? "alert" : undefined,
    },
    emitting: {
      // The air and land equivalent of the AIS question: is it announcing itself at all.
      value: typeof p.emitting === "boolean" ? (p.emitting ? "yes" : "NO") : null,
      tone: p.emitting === false ? "alert" : undefined,
    },
    confirmed: {
      // 🔒 Spelled out rather than left to the ring: one bucket is a link to fix, the other
      // is a contact nothing can see, and they want different answers.
      value:
        unknownState(asset) === "detected_not_reported"
          ? "NO, report cannot reach us"
          : unknownState(asset) === "untracked"
            ? "NO, held by nothing"
            : null,
      tone: "alert",
    },
    held_by: { value: heldBy },
    speed_kmh: { value: speedOf(asset) },
    heading_deg: { value: num(p.heading_deg) === null ? null : `${num(p.heading_deg)}\u00b0` },
    alt_m: { value: altitude === null ? null : `${altitude} m` },
    detection_radius_km: {
      // Top level, not props: the server computes the EFFECTIVE range, because a node's
      // comes from its payload through the sensor table rather than from the seed.
      value:
        typeof asset.detectionRadiusKm === "number" ? `${asset.detectionRadiusKm} km` : null,
    },
    flight_radius_km: {
      value: num(p.flight_radius_km) === null ? null : `${num(p.flight_radius_km)} km`,
    },
    payload: { value: str(p.payload) },
    cluster_name: { value: str(p.cluster_name) },
    backhaul: { value: str(p.backhaul) },
    position: { value: positionOf(asset) },
  };

  // 🔑 EVERY ASSET SHOWS EVERY FIELD, IN ONE ORDER, AND N/A WHERE IT DOES NOT APPLY.
  // Ethan's call, and the reason is stronger than tidiness: a panel that lists only what
  // happens to be populated cannot tell an operator the difference between "this has no
  // depth" and "nobody filled that in". Dropping empty rows made absence and decision look
  // identical on screen, which is the same fault the domain model had.
  //
  // ⚠️ IT PROVED ITSELF IMMEDIATELY. With every row shown, `held by` reads N/A on a ground
  // party while the map draws three dotted lines from sensors holding it. That contradiction
  // was always there; filtering the empty rows is what kept it off the screen.
  const rowsOf = (schema ?? []).map((f) => ({
    spec: f,
    applies: f.applies.length === 0 || f.applies.includes(asset.kind),
  }));

  const shown: Row[] = (schema ?? []).map((f) => ({
    label: f.label,
    title: f.note || undefined,
    ...(f.applies.length === 0 || f.applies.includes(asset.kind)
      ? (values[f.name] ?? { value: null })
      : { value: null }),
  }));

  return (
    <aside className="banner" aria-label={`Details for ${asset.name}`}>
      <header>
        <span className="bname">{asset.name}</span>
        <button className="bclose" onClick={() => select(null)} aria-label="Close details">
          ✕
        </button>
      </header>

      <div className="bkind">
        {KIND_LABEL[asset.kind] ?? asset.kind}
        {/* A role rather than a condition, so it is styled apart from the ring flags: this
            is the box the rest of its cluster relays through to get off the ice. */}
        {isGateway(asset) && (
          <span
            className="bflag gateway"
            title="carries the satellite backhaul its cluster relays through"
          >
            BACKHAUL
          </span>
        )}
        {ring && <span className={`bflag ${ring}`}>{RING_LABEL[ring]}</span>}
      </div>

      <dl>
        {shown.map((r, i) => {
          const meta = rowsOf[i];
          // 🔑 THE SERVER DECIDES WHAT MAY BE EDITED. `editable` comes off the declaration,
          // where it is derived from the field's origin: observed and derived values never
          // get a pencil, because editing what a sensor reported falsifies the record and a
          // derived value would simply disagree with its inputs on the next read.
          const canEdit = meta?.applies && meta.spec.editable;
          return (
          <div key={r.label} className="brow">
            <dt title={r.title}>{r.label}</dt>
            {/* N/A is styled down: it is the absence of a fact, not a fact, and it must not
                read with the same weight as a real value on a dense panel. */}
            {editing === meta?.spec.name ? (
              <dd>
                <FieldEditor
                  spec={meta.spec}
                  asset={asset}
                  onDone={() => setEditing(null)}
                />
              </dd>
            ) : r.value === null || r.value === undefined ? (
              <dd className="na">
                {canEdit && (
                  <button className="bedit" onClick={() => setEditing(meta.spec.name)}
                          title={`set ${meta.spec.label}`} aria-label={`Edit ${meta.spec.label}`}>
                    ✎
                  </button>
                )}
                N/A
              </dd>
            ) : (
              <dd className={r.tone}>
                {canEdit && (
                  <button className="bedit" onClick={() => setEditing(meta.spec.name)}
                          title={`change ${meta.spec.label}`} aria-label={`Edit ${meta.spec.label}`}>
                    ✎
                  </button>
                )}
                {r.value}
              </dd>
            )}
          </div>
          );
        })}
      </dl>
    </aside>
  );
}
