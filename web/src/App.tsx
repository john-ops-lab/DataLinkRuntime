import { useEffect, useState } from "react";

type HealthStatus = "loading" | "ok" | "degraded" | "unreachable";

interface HealthPayload {
  status: string;
  database: boolean;
}

// A valid health payload is mapped by its status field (503 responses with a
// legal payload count as "degraded"). Anything without a valid payload is
// treated as unreachable.
function toHealthStatus(payload: HealthPayload): HealthStatus {
  if (payload.status === "ok") {
    return "ok";
  }
  if (payload.status === "degraded") {
    return "degraded";
  }
  return "unreachable";
}

export default function App() {
  const [health, setHealth] = useState<HealthStatus>("loading");

  useEffect(() => {
    let cancelled = false;

    async function checkHealth() {
      try {
        const response = await fetch("/api/health");
        const payload = (await response.json()) as HealthPayload;
        if (!cancelled) {
          setHealth(toHealthStatus(payload));
        }
      } catch {
        if (!cancelled) {
          setHealth("unreachable");
        }
      }
    }

    void checkHealth();
    return () => {
      cancelled = true;
    };
  }, []);

  const statusText =
    health === "loading"
      ? "Checking control health..."
      : health === "ok"
        ? "Control: ok"
        : health === "degraded"
          ? "Control: degraded"
          : "Control: unreachable";

  return (
    <main>
      <h1>DataLinkRuntime</h1>
      <p data-testid="control-status">{statusText}</p>
    </main>
  );
}
