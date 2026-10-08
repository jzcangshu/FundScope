import { createEvent, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { DragEvent as ReactDragEvent } from "react";
import { AuthProvider } from "@/app/auth";
import { AppLayout, preventSidebarDragStart } from "@/app/layout";
import { ThemeProvider } from "@/app/theme";
import * as authApi from "@/api/auth";

vi.mock("@/api/auth", () => ({
  authStatus: vi.fn(),
  login: vi.fn(),
  initialize: vi.fn(),
  me: vi.fn(),
  logout: vi.fn(),
  changePassword: vi.fn(),
}));

vi.mock("@/components", () => ({
  CacheStatus: () => null,
  UpdateBanner: () => null,
}));

const admin = {
  id: 1,
  username: "admin",
  display_name: "系统管理员",
  role: "admin" as const,
  fund_access_mode: "all" as const,
  fund_ids: [],
  status: "active" as const,
  last_login_at: null,
  navigation: ["dashboard"],
};

beforeAll(() => {
  // antd's responsive observer needs matchMedia, which jsdom does not provide.
  if (!window.matchMedia) {
    window.matchMedia = ((query: string) =>
      ({
        matches: false,
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      }) as unknown as MediaQueryList) as typeof window.matchMedia;
  }
});

function renderLayout() {
  return render(
    <MemoryRouter initialEntries={["/dashboard"]}>
      <AuthProvider>
        <ThemeProvider>
          <AppLayout />
        </ThemeProvider>
      </AuthProvider>
    </MemoryRouter>,
  );
}

describe("preventSidebarDragStart", () => {
  const makeEvent = (target: EventTarget, currentTarget: HTMLElement) =>
    ({ target, currentTarget, preventDefault: vi.fn() }) as unknown as ReactDragEvent<HTMLElement>;

  it("cancels drags that originate inside the sidebar DOM", () => {
    const currentTarget = document.createElement("div");
    const target = document.createElement("a");
    currentTarget.appendChild(target);
    const event = makeEvent(target, currentTarget);

    preventSidebarDragStart(event);

    expect(event.preventDefault).toHaveBeenCalledTimes(1);
  });

  it("leaves drags from portal content mounted outside the sidebar DOM alone", () => {
    const currentTarget = document.createElement("div");
    const target = document.body.appendChild(document.createElement("div"));
    const event = makeEvent(target, currentTarget);

    preventSidebarDragStart(event);

    expect(event.preventDefault).not.toHaveBeenCalled();
  });

  it("ignores targets that are not DOM nodes", () => {
    const event = makeEvent(window, document.createElement("div"));

    preventSidebarDragStart(event);

    expect(event.preventDefault).not.toHaveBeenCalled();
  });
});

describe("AppLayout sidebar drag hardening", () => {
  beforeEach(() => {
    vi.mocked(authApi.authStatus).mockResolvedValue({ initialized: true });
    vi.mocked(authApi.me).mockResolvedValue(admin);
  });

  it("marks nav links and the sidebar brand as non-draggable, keeping the mobile bar untouched", async () => {
    renderLayout();

    const navLink = await screen.findByRole("link", { name: "公司总览" });
    expect(navLink).toHaveAttribute("draggable", "false");

    const brands = screen.getAllByLabelText("FundScope 首页");
    expect(brands).toHaveLength(2);
    const sidebarBrand = brands.find((el) => el.closest("aside"));
    const mobileBrand = brands.find((el) => !el.closest("aside"));
    expect(sidebarBrand).toHaveAttribute("draggable", "false");
    expect(mobileBrand).toHaveAttribute("draggable", "true");
  });

  it("cancels a dragstart raised inside the sidebar nav", async () => {
    renderLayout();

    const navLink = await screen.findByRole("link", { name: "风险事件" });
    const event = createEvent.dragStart(navLink);
    fireEvent(navLink, event);

    expect(event.defaultPrevented).toBe(true);
  });
});
