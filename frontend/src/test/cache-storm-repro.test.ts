import { act, renderHook, waitFor } from "@testing-library/react";
import { apiRequest, ApiError } from "@/api/client";
import { useAsyncData } from "@/components/hooks";

describe("read request notification safety", () => {
  it.each([false, true])("does not create reload storms when storage is broken: %s", async (broken) => {
    const storage = vi.spyOn(Storage.prototype, "setItem");
    if (broken) storage.mockImplementation(() => { throw new DOMException("QuotaExceededError"); });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(JSON.stringify({ data: {} }), { headers: { "content-type": "application/json" } }));
    const paths = ["/dashboard/overview", "/dashboard/series", "/funds?status=active"];
    const { result } = renderHook(() => useAsyncData(() => Promise.all(paths.map((path) => apiRequest(path))), [], "失败"));
    await waitFor(() => expect(result.current.loading).toBe(false));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(storage).not.toHaveBeenCalled();
  });
  it("removes already-rendered data after an authorization denial, including silent refresh", async () => {
    const loader = vi.fn().mockResolvedValueOnce({ name: "previously-authorized" }).mockRejectedValueOnce(new ApiError(404, "Not found"));
    const { result } = renderHook(() => useAsyncData(loader, [], "加载失败"));
    await waitFor(() => expect(result.current.data).toEqual({ name: "previously-authorized" }));
    await act(async () => { await result.current.reload({ silent: true }); });
    expect(result.current.data).toBeNull();
    expect(result.current.error).toBeTruthy();
  });

});
