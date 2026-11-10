import StatusBadge from "./StatusBadge";

/** Badge tone keys for DecisionExplanation.outcome (server-derived, deterministic). */
const OUTCOME_KIND = { pass: "pass", warning: "warning", fail: "fail", blocked: "blocked",
  needs_clarification: "needs_clarification", not_run: "info" };
const OUTCOME_LABEL = { not_run: "Not run" };

/** Renders the backend's structured "why" for each decision. It never computes outcomes itself. */
export default function DecisionExplanations({ explanations }) {
  const list = Array.isArray(explanations) ? explanations.filter((e) => e && typeof e === "object") : [];
  if (!list.length) return null;
  return (
    <div className="stack-sm" data-testid="explanations">
      <p className="small muted">Each answer is built from the recorded checks, so it always matches the outcome shown. No AI rewording is applied.</p>
      {list.map((e) => (
        <details key={e.topic} className="disclosure" data-topic={e.topic}>
          <summary>
            <span>Why? {e.title}</span>{" "}
            <StatusBadge kind="outcome" status={OUTCOME_KIND[e.outcome] || e.outcome} label={OUTCOME_LABEL[e.outcome]} />
          </summary>
          <div className="stack-sm">
            <p>{e.summary}</p>
            {(e.factors || []).length > 0 && (
              <ul className="stack-sm small" aria-label={`${e.title} factors`}>
                {e.factors.map((f, i) => (
                  <li key={`${f.label}-${i}`}>
                    <StatusBadge kind="outcome" status={f.status} /> <strong>{f.label}</strong>{f.detail ? `: ${f.detail}` : ""}
                    {(f.evidence || []).length > 0 && <span className="muted"> · Evidence: <span className="mono">{f.evidence.join(", ")}</span></span>}
                  </li>
                ))}
              </ul>
            )}
            {(e.limitations || []).length > 0 && (
              <p className="small muted">{e.limitations.join(" ")}{e.policy_set_version != null ? ` Policy set v${e.policy_set_version}.` : ""}</p>
            )}
          </div>
        </details>
      ))}
    </div>
  );
}
