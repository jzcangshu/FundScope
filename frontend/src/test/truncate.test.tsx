import { describe, expect, it } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { Truncate } from "@/components";

describe("Truncate", () => {
  it("renders the empty placeholder for blank values", () => {
    const { container } = render(<Truncate value={null} />);
    expect(container.textContent).toBe("—");
  });

  it("shows short text verbatim with no toggle", () => {
    render(<Truncate value="短文本" maxChars={80} />);
    expect(screen.getByText("短文本")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("collapses text longer than maxChars with a 展开 toggle", () => {
    render(<Truncate value={"a".repeat(120)} maxChars={20} />);
    // Preview shows the first maxChars characters plus an ellipsis.
    expect(screen.getByText(/^a{20}…$/)).toBeInTheDocument();
    const toggle = screen.getByRole("button", { name: "展开" });
    fireEvent.click(toggle);
    expect(screen.getByText(/^a{120}$/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "收起" })).toBeInTheDocument();
  });

  it("stringifies object values before truncating", () => {
    render(<Truncate value={{ k: "v" }} maxChars={200} />);
    expect(screen.getByText('{"k":"v"}')).toBeInTheDocument();
  });
});