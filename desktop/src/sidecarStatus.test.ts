import { describe, expect, it } from "vitest";
import {
  canSendRequests,
  deriveSidecarStatus,
  textLeaksSecret,
} from "./sidecarStatus";

describe("deriveSidecarStatus", () => {
  it("is starting when neither baseUrl nor error is set", () => {
    expect(deriveSidecarStatus({ baseUrl: null, error: null })).toBe(
      "starting",
    );
  });

  it("is ready once baseUrl is known", () => {
    expect(
      deriveSidecarStatus({ baseUrl: "http://127.0.0.1:54321", error: null }),
    ).toBe("ready");
  });

  it("is error when an error is present, even if baseUrl was set earlier", () => {
    expect(
      deriveSidecarStatus({
        baseUrl: "http://127.0.0.1:54321",
        error: "sidecar crashed",
      }),
    ).toBe("error");
  });
});

describe("canSendRequests", () => {
  it("is false while starting", () => {
    expect(canSendRequests({ baseUrl: null, error: null })).toBe(false);
  });

  it("is true only once ready", () => {
    expect(
      canSendRequests({ baseUrl: "http://127.0.0.1:1234", error: null }),
    ).toBe(true);
  });

  it("is false on error", () => {
    expect(
      canSendRequests({ baseUrl: "http://127.0.0.1:1234", error: "boom" }),
    ).toBe(false);
  });
});

describe("textLeaksSecret", () => {
  it("detects the raw key inside arbitrary text", () => {
    expect(textLeaksSecret("failed: key sk-abc123 rejected", "sk-abc123")).toBe(
      true,
    );
  });

  it("is false when the key does not appear", () => {
    expect(textLeaksSecret("sidecar did not respond in time", "sk-abc123")).toBe(
      false,
    );
  });

  it("ignores an empty secret", () => {
    expect(textLeaksSecret("some log line", "")).toBe(false);
  });
});
