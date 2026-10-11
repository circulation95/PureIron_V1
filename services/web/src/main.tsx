import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider } from "react-router";

import { router } from "./App";
import "uplot/dist/uPlot.min.css";
import "./styles.css";

const root = document.getElementById("root");
if (root === null) {
  throw new Error("#root element missing");
}
createRoot(root).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>,
);
