import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Quality, { parseRows } from "./Quality";
import type { Client } from "../api";

afterEach(cleanup);

describe("parseRows", () => {
  it("reads CSV with optional flags", () => {
    expect(parseRows("true,pred,group,leak\n1,0,A,yes\n0,0,B,")).toEqual([
      { true: 1, pred: 0, group: "A", unsupported: false, leak: true },
      { true: 0, pred: 0, group: "B", unsupported: false, leak: false },
    ]);
  });
  it("reads JSON arrays and {records}", () => {
    expect(parseRows('{"records":[{"true":1,"pred":1,"group":"A"}]}')).toHaveLength(1);
  });
  it("rejects bad values with the row number", () => {
    expect(() => parseRows("true,pred,group\n1,2,A")).toThrow(/Row 2/);
    expect(() => parseRows("label,pred,group\n1,1,A")).toThrow(/true/);
  });
});

describe("Quality view", () => {
  it("explains a hidden subgroup failure in plain language", async () => {
    const scorecard = vi.fn(async () => ({
      n: 400, accuracy: 0.95, accuracy_ci95: [0.92, 0.97], precision: 1, precision_ci95: [0.98, 1],
      recall: 0.9, recall_ci95: [0.85, 0.93], f1: 0.947, unsupported_rate: 0, leaks: 0,
      selection_rate_gap: 0.2, recall_gap: 0.4, decision: "HOLD", failed_gates: ["group_recall:B", "recall_gap"],
      groups: { A: { n: 300, positives: 150, tp: 150, fp: 0, tn: 150, fn: 0, accuracy: 1, accuracy_ci95: [0.99, 1], precision: 1, precision_ci95: [0.97, 1], recall: 1, recall_ci95: [0.97, 1], f1: 1, positive_rate: 0.5 },
                B: { n: 100, positives: 50, tp: 30, fp: 0, tn: 50, fn: 20, accuracy: 0.8, accuracy_ci95: [0.71, 0.87], precision: 1, precision_ci95: [0.89, 1], recall: 0.6, recall_ci95: [0.46, 0.72], f1: 0.75, positive_rate: 0.3 } },
    }));
    render(<Quality client={{ scorecard } as unknown as Client} />);
    await userEvent.click(screen.getByRole("button", { name: /hidden subgroup/ }));
    await userEvent.click(screen.getByRole("button", { name: "Run scorecard" }));
    expect(scorecard).toHaveBeenCalledWith(expect.arrayContaining([{ true: 1, pred: 1, group: "A", unsupported: false, leak: false }]));
    expect((scorecard.mock.calls[0] as unknown[])[0]).toHaveLength(400);
    expect(await screen.findByText(/Recall for “B” is below 0.70/)).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Hold");
  });

  it("refuses fewer than 20 rows before calling the API", async () => {
    const scorecard = vi.fn();
    render(<Quality client={{ scorecard } as unknown as Client} />);
    await userEvent.type(screen.getByRole("textbox"), "true,pred,group{enter}1,1,A");
    await userEvent.click(screen.getByRole("button", { name: "Run scorecard" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Need 20");
    expect(scorecard).not.toHaveBeenCalled();
  });
});
