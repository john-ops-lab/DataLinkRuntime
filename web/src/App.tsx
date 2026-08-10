import { useEffect, useState } from "react";

type HealthStatus = "loading" | "ok" | "unreachable";

interface HealthPayload {
  status: string;
  database: boolean;
}

export default function App() {
  const [health, setHealth] = useState<HealthStatus>("loading");

  useEffect(() => {
    let cancelled = false;

    async function checkHealth() {
      try {
        const response = await fetch("/api/health");
        if (!response.ok) {
          throw new Error(`unexpected status ${response.status}`);
        }
        const payload = (await response.json()) as HealthPayload;
        if (!cancelled) {
          setHealth(payload.status === "ok" && payload.database ? "ok" : "unreachable");
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
        : "Control: unreachable";

  return (
    <main>
      <h1>DataLinkRuntime</h1>
      <p data-testid="control-status">{statusText}</p>
    </main>
  );
}
