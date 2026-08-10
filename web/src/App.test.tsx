import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";

function mockFetchOnce(payload: unknown, ok = true) {
  return vi.fn().mockResolvedValue({
    ok,
    json: async () => payload,
  });
}

describe("App", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows control ok when the health endpoint reports ok", async () => {
    vi.stubGlobal("fetch", mockFetchOnce({ status: "ok", database: true }));
    render(<App />);
    await waitFor(() => {
      expect(screen.getByTestId("control-status").textContent).toBe("Control: ok");
    });
  });

  it("shows unreachable when the health request fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")));
    render(<App />);
    await waitFor(() => {
      expect(screen.getByTestId("control-status").textContent).toBe("Control: unreachable");
    });
  });
});
