import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App as AntdApp } from "antd";
import { RouterProvider } from "react-router-dom";
import { ThemeProvider } from "@/app/theme";
import { AuthProvider } from "@/app/auth";
import { CrashRecovery } from "@/app/CrashRecovery";
import { watchGlobalErrors } from "@/app/crashReporting";
import { ConfirmProvider } from "@/components";
import { router } from "@/app/router";
// Self-hosted Latin subsets only (~70 KB): figures and Latin text render in
// Inter / JetBrains Mono; CJK falls through to the system sans in the stack.
import "@fontsource-variable/inter/standard.css";
import "@fontsource/jetbrains-mono/500.css";
import "@/styles/globals.css";

// Wired before the first render so a failure during mount is still observable.
watchGlobalErrors();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider>
      <CrashRecovery>
        <AntdApp>
          <AuthProvider>
            <ConfirmProvider>
              <RouterProvider router={router} />
            </ConfirmProvider>
          </AuthProvider>
        </AntdApp>
      </CrashRecovery>
    </ThemeProvider>
  </StrictMode>,
);