import "./styles/index.css";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { HashRouter, Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "./components/AppShell";
import { HomePage } from "./pages/HomePage";
import { ProjectPage } from "./pages/ProjectPage";
import { ProjectsPage } from "./pages/ProjectsPage";
import { SettingsPage } from "./pages/SettingsPage";
import { CharactersPage } from "./pages/CharactersPage";

createRoot(document.getElementById("root")!).render(
  <StrictMode><HashRouter><AppShell><Routes>
    <Route path="/" element={<HomePage />} />
    <Route path="/projects" element={<ProjectsPage />} />
    <Route path="/project/:id" element={<ProjectPage />} />
    <Route path="/settings" element={<SettingsPage />} />
    <Route path="/characters" element={<CharactersPage />} />
    <Route path="*" element={<Navigate to="/" replace />} />
  </Routes></AppShell></HashRouter></StrictMode>,
);
