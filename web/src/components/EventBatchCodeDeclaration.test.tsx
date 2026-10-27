import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { applyUiLocale } from "../i18n";
import EventBatchCodeDeclaration from "./EventBatchCodeDeclaration";

beforeEach(async () => { await applyUiLocale("en"); });

it("changes only the draft marker and preserves arbitrary existing configuration", () => {
  const change = vi.fn();
  const existing = { nested: { retained: [1, true, null] }, timeout: 42, historical_key: "old-value" };
  const view = render(<EventBatchCodeDeclaration value={JSON.stringify(existing)} disabled={false} onChange={change} />);
  expect(change).not.toHaveBeenCalled();
  fireEvent.click(screen.getByLabelText("This code supports several events per run"));
  expect(change).toHaveBeenCalledOnce();
  expect(JSON.parse(change.mock.calls[0][0])).toEqual({ ...existing, event_batch_schema: "dlr.event.batch.v1" });
  view.rerender(<EventBatchCodeDeclaration value={change.mock.calls[0][0]} disabled={false} onChange={change} />);
  fireEvent.click(screen.getByLabelText("This code supports several events per run"));
  expect(JSON.parse(change.mock.calls[1][0])).toEqual(existing);
});

it.each(["{broken", "null", "[]", '"legacy"'])("leaves invalid or non-object draft %s untouched", value => {
  const change = vi.fn();
  render(<EventBatchCodeDeclaration value={value} disabled={false} onChange={change} />);
  expect((screen.getByRole("checkbox") as HTMLInputElement).disabled).toBe(true);
  expect(screen.getByText(/will not overwrite the draft/)).toBeTruthy();
  fireEvent.click(screen.getByRole("checkbox"));
  expect(change).not.toHaveBeenCalled();
});

it("keeps an unsupported old marker untouched until explicit author action", () => {
  const change = vi.fn();
  const value = '{"event_batch_schema":"old-contract","retained":9}';
  const view = render(<EventBatchCodeDeclaration value={value} disabled={false} onChange={change} />);
  expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(false);
  view.unmount();
  expect(change).not.toHaveBeenCalled();
});

it("preserves a saved declaration while locked and does not publish a change", () => {
  const change = vi.fn();
  render(<EventBatchCodeDeclaration value='{"event_batch_schema":"dlr.event.batch.v1","retained":true}' disabled onChange={change} />);
  expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(true);
  expect((screen.getByRole("checkbox") as HTMLInputElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("checkbox"));
  expect(change).not.toHaveBeenCalled();
});

it("discarding the controlled draft does not save the declaration", () => {
  const change = vi.fn();
  const view = render(<EventBatchCodeDeclaration value="{}" disabled={false} onChange={change} />);
  fireEvent.click(screen.getByRole("checkbox"));
  view.rerender(<EventBatchCodeDeclaration value={change.mock.calls[0][0]} disabled={false} onChange={change} />);
  expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(true);
  view.rerender(<EventBatchCodeDeclaration value="{}" disabled={false} onChange={change} />);
  expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(false);
  expect(change).toHaveBeenCalledOnce();
});

it.each([
  ["en", '{"nested":[9007199254740993]}', false, /Displayed values may be inaccurate/],
  ["zh-CN", '{"nested":[-9007199254740993]}', false, /当前显示可能不精确/],
  ["en", '{"nested":[1e400]}', false, /Displayed values may be inaccurate/],
  ["zh-CN", '{"nested":[null]}', true, /当前显示可能不精确/],
] as const)("blocks unsafe automatic edits in %s without changing the draft", async (locale, value, unsafeNumbers, hint) => {
  await applyUiLocale(locale);
  const change = vi.fn();
  render(<EventBatchCodeDeclaration value={value} unsafeNumbers={unsafeNumbers} disabled={false} onChange={change} />);
  expect((screen.getByRole("checkbox") as HTMLInputElement).disabled).toBe(true);
  expect(screen.getByText(hint)).toBeTruthy();
  fireEvent.click(screen.getByRole("checkbox"));
  expect(change).not.toHaveBeenCalled();
});

it("preserves safe numeric boundaries, finite decimals and numeric strings when declaring support", () => {
  const config = { nested: [Number.MAX_SAFE_INTEGER, Number.MIN_SAFE_INTEGER, 0.1, "9007199254740993"] };
  const change = vi.fn();
  render(<EventBatchCodeDeclaration value={JSON.stringify(config)} disabled={false} onChange={change} />);
  fireEvent.click(screen.getByRole("checkbox"));
  expect(JSON.parse(change.mock.calls[0][0])).toEqual({ ...config, event_batch_schema: "dlr.event.batch.v1" });
});
