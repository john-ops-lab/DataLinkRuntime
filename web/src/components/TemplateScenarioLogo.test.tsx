import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import TemplateScenarioLogo, { TEMPLATE_LOGO_KEYS } from "./TemplateScenarioLogo";

it("renders every allowlisted local logo tile without remote media", () => {
  const { container } = render(
    <div>
      {TEMPLATE_LOGO_KEYS.map((key) => <TemplateScenarioLogo key={key} logoKey={key} />)}
    </div>,
  );

  expect(container.querySelectorAll(".template-logo-tile")).toHaveLength(17);
  expect(container.querySelectorAll("img, image")).toHaveLength(0);
  for (const key of TEMPLATE_LOGO_KEYS) {
    expect(container.querySelector(`[data-logo-key="${key}"]`)?.getAttribute("aria-hidden")).toBe("true");
  }
});

it("uses the DLR fallback for an unknown defensive key", () => {
  render(<TemplateScenarioLogo logoKey="unknown-key" />);
  expect(screen.getByText("DLR")).toBeTruthy();
});
