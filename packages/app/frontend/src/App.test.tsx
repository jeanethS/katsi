// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "./App";

describe("Status screen", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    window.location.hash = "";
  });

  it("shows status returned by the backend", async () => {
    window.location.hash = "status";
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        ollama: { reachable: true, models: ["embed-model", "chat-model"] },
        counts: { files: 12, chunks: 34, entities: 56, topics: 78 },
        db_bytes: { vectors: 1, graph: 2 },
        synth: { backend: "local", cloud_configured: false },
      }),
    }));

    render(<App />);

    expect(await screen.findByText("embed-model / chat-model")).toBeInTheDocument();
    expect(screen.getByText("12 files / 34 chunks / 56 entities / 78 topics")).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith("/api/status", expect.objectContaining({ signal: expect.any(AbortSignal) }));
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("retries after the backend becomes reachable", async () => {
    window.location.hash = "status";
    vi.stubGlobal("fetch", vi.fn()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({
          ollama: { reachable: false, models: [] },
          counts: { files: 0, chunks: 0, entities: 0, topics: 0 },
          db_bytes: { vectors: 0, graph: 0 },
          synth: { backend: "return_only", cloud_configured: false },
        }),
      }));

    render(<App />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByText("Ollama is not reachable")).toBeInTheDocument();
    expect(document.querySelector(".status-dot.is-error")).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledTimes(2);
  });
});

describe("Library screen", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    window.location.hash = "";
  });

  it("adds a folder and indexes it through the backend", async () => {
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ nodes: [], edges: [] }) })
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ indexed: 2, skipped: 0, error: 0, total: 2 }) }));

    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "Add folder" }));
    fireEvent.change(screen.getByLabelText("Folder path"), { target: { value: "/tmp/project-notes" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    const folder = screen.getByText("project-notes").closest("section");
    expect(folder).not.toBeNull();
    fireEvent.click(within(folder!).getByRole("button", { name: "Index now" }));

    expect(await screen.findByText("2 / 2 files indexed")).toBeInTheDocument();
    expect(fetch).toHaveBeenLastCalledWith("/api/index", expect.objectContaining({ method: "POST" }));
  });
});
