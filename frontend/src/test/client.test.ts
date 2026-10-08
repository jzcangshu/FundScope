import { apiRequest, ApiError, setUnauthorizedHandler } from "@/api/client";

describe("apiRequest", () => {
  it("uses same-origin credentials and serializes JSON", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ data: { ok: true } }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    await expect(
      apiRequest<{ data: { ok: boolean } }>("/auth/login", {
        method: "POST",
        body: { username: "operator", password: "secret-value" },
      }),
    ).resolves.toEqual({ data: { ok: true } });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/auth/login",
      expect.objectContaining({
        credentials: "include",
        method: "POST",
        body: JSON.stringify({ username: "operator", password: "secret-value" }),
        headers: expect.any(Headers),
      }),
    );
    const headers = fetchMock.mock.calls[0][1]?.headers as Headers;
    expect(headers.get("content-type")).toBe("application/json");
  });

  it("passes FormData without setting a content type", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ data: {} }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    const body = new FormData();
    body.append("file", new Blob(["xls"]), "valuation.xls");

    await apiRequest("/imports", { method: "POST", body });

    const init = fetchMock.mock.calls[0][1];
    expect(init?.body).toBe(body);
    expect((init?.headers as Headers).has("content-type")).toBe(false);
  });

  it("returns undefined for a 204 response", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 204 }));

    await expect(apiRequest<void>("/auth/logout", { method: "POST" })).resolves.toBeUndefined();
  });

  it("returns binary responses as blobs", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("excel-bytes", {
        status: 200,
        headers: { "content-type": "application/vnd.ms-excel" },
      }),
    );

    const result = await apiRequest<Blob>("/imports/1/source/2");

    // Duck-type rather than instanceof: vitest's jsdom Blob and the
    // undici Blob returned by response.blob() in Node are distinct
    // realms, so a literal instanceof check would fail in CI.
    expect(result).toBeTruthy();
    expect(typeof result.text).toBe("function");
    expect(result.size).toBe(11);
    await expect(result.text()).resolves.toBe("excel-bytes");
  });

  it("throws a safe ApiError and notifies on 401", async () => {
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: [{ msg: "Invalid input", input: "super-secret-password" }],
        }),
        { status: 401, headers: { "content-type": "application/json" } },
      ),
    );

    const request = apiRequest("/auth/login", {
      method: "POST",
      body: { password: "super-secret-password" },
    });

    await expect(request).rejects.toMatchObject({
      name: "ApiError",
      status: 401,
      detail: "Invalid input",
    });
    await expect(request).rejects.not.toThrow("super-secret-password");
    expect(onUnauthorized).toHaveBeenCalledOnce();
    setUnauthorizedHandler(null);
  });

  it("uses a generic message for non-JSON server errors", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("proxy failure with internal details", { status: 502 }),
    );

    await expect(apiRequest("/auth/me")).rejects.toEqual(
      new ApiError(502, "请求失败（502）"),
    );
  });
});
