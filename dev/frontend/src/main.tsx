import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { installAssetRecovery } from "./assetRecovery";
import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/pages.css";
import "./styles/components.css";
import "./styles/layout.css";
import "./styles/type-scale.css";
import "./styles/knowledge-detail.css";
import "./styles/finding-detail.css";

installAssetRecovery();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>
);
