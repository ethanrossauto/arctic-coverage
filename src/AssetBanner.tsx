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

export function AssetBanner() {
  const selectedId = useStore((s) => s.selectedId);
  const assets = useStore((s) => s.assets);
  const mesh = useStore((s) => s.mesh);
  const select = useStore((s) => s.select);
  // ⚠️ ABOVE THE EARLY RETURN, because hooks cannot run conditionally. Reading the clock
  // used to happen after it, which was legal only because `Date.now()` is not a hook.
  const now = useNow();

  const asset: Asset | undefined = assets.find((a) => a.id === selectedId);
  if (!asset) return null;

  const ring = ringState(asset, weakAssetIds(mesh), now);
  const heard = minutesSinceHeard(asset, now);
  const connections = connectionCount(asset.id, mesh);
  const p = asset.props ?? {};

  const str = (v: unknown): string | null => (typeof v === "string" && v ? v : null);
  const num = (v: unknown): number | null => (typeof v === "number" ? v : null);

  // Who is holding this contact, under whichever name the seed currently uses.
  const heldBy = str(p.held_by) ?? str(p.first_detected_by);
  const battery = num(p.battery_pct);
  const altitude = num(p.altitude_m) ?? asset.altM;

  const rows: Row[] = [
    {
      label: "last heard",
      value: heard === null ? null : ago(heard),
      title: "how long since this asset reported",
      tone: ring === "unreachable" ? "warn" : undefined,
    },
    {
      label: "connections",
      // Radar sites are not on the mesh at all, so zero is a fact about the network for a
      // node and a meaningless one for them. Only shown where the question applies.
      value: asset.kind === "radar" ? null : connections === 0 ? "none" : connections,
      title: "how many assets it can reach directly on the mesh right now",
      tone: connections === 0 ? "warn" : undefined,
    },
    {
      label: "in range of",
      // Its own row rather than a fourth ring: being out of radio range is a different
      // problem from not reaching us, and it wants a different answer.
      value: asset.meshConnected === false ? "nothing" : null,
      title: "no neighbour is close enough to talk to, whatever its health",
      tone: "warn",
    },
    {
      label: "avg connected",
      value:
        num(p.avg_gateway_minutes) === null ? null : duration(num(p.avg_gateway_minutes)!),
      title: "mean length of an unbroken spell with a live path to a gateway",
    },
    {
      label: "broadcasting",
      value: asset.aisReporting === null ? null : asset.aisReporting ? "yes, AIS" : "NO AIS",
      tone: asset.aisReporting === false ? "alert" : undefined,
    },
    {
      label: "emitting",
      // The air and land equivalent of the AIS question: is it announcing itself at all.
      value: typeof p.emitting === "boolean" ? (p.emitting ? "yes" : "NO") : null,
      tone: p.emitting === false ? "alert" : undefined,
    },
    {
      label: "confirmed",
      // 🔒 Spelled out rather than left to the ring, because the two buckets have different
      // answers: one is a link to fix, the other is a contact nothing can see.
      value:
        unknownState(asset) === "detected_not_reported"
          ? "NO, report cannot reach us"
          : unknownState(asset) === "untracked"
            ? "NO, held by nothing"
            : null,
      title: "whether the sensor network can actually confirm this contact",
      tone: "alert",
    },
    {
      label: "held by",
      // Provenance is what makes an unidentified contact actionable rather than alarming.
      value: heldBy,
      title: "the sensor holding this contact",
    },
    { label: "track from", value: str(p.track_source) },
    { label: "class", value: str(p.classification) },
    { label: "speed", value: speedOf(asset) },
    { label: "heading", value: num(p.heading_deg) === null ? null : `${num(p.heading_deg)}°` },
    { label: "altitude", value: altitude === null ? null : `${altitude} m` },
    { label: "depth", value: num(p.depth_m) === null ? null : `${num(p.depth_m)} m` },
    {
      label: "battery",
      value: battery === null ? null : `${battery}%`,
      tone: battery !== null && battery < 30 ? "warn" : undefined,
    },
    {
      label: "endurance",
      value:
        num(p.endurance_min_remaining) === null
          ? null
          : duration(num(p.endurance_min_remaining)!),
      title: "flight time remaining",
    },
    { label: "state", value: str(p.state) },
    { label: "party", value: num(p.party_size) ?? num(p.members) },
    { label: "transport", value: str(p.transport) },
    { label: "next", value: str(p.next_waypoint) },
    {
      label: "detects to",
      value: num(p.detection_radius_km) === null ? null : `${num(p.detection_radius_km)} km`,
      title: "how far this sensor can hold a contact",
    },
    { label: "range", value: num(p.range_km) === null ? null : `${num(p.range_km)} km` },
    { label: "operator", value: str(p.operator) },
    // ⚠️ `props.flag` is the vessel's COUNTRY and has nothing to do with the asset's
    // condition flag. Labelled "registry" so the two senses never collide on screen.
    { label: "registry", value: str(p.flag) },
    { label: "cluster", value: str(p.cluster_name) ?? str(p.cluster) },
    { label: "payload", value: str(p.payload) },
    { label: "position", value: positionOf(asset), title: "latitude and longitude" },
  ];

  const shown = rows.filter((r) => r.value !== null && r.value !== undefined);

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
        {shown.map((r) => (
          <div key={r.label} className="brow">
            <dt title={r.title}>{r.label}</dt>
            <dd className={r.tone}>{r.value}</dd>
          </div>
        ))}
      </dl>
    </aside>
  );
}
