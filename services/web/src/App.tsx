// 화면 경로(plan M3b §2.5). 로그인 밖의 경로는 모두 Layout이 세션을 확인한다. 편집 화면은 없다(No config UI).
import { createBrowserRouter, Navigate } from "react-router";

import { Layout } from "./app/Layout";
import { Alarms } from "./pages/Alarms";
import { Audit } from "./pages/Audit";
import { Devices } from "./pages/Devices";
import { Login } from "./pages/Login";
import { Overview } from "./pages/Overview";
import { Trend } from "./pages/Trend";

export const router = createBrowserRouter([
  { path: "/login", element: <Login /> },
  {
    path: "/",
    element: <Layout />,
    children: [
      { index: true, element: <Overview /> },
      { path: "alarms", element: <Alarms /> },
      { path: "trend", element: <Trend /> },
      { path: "devices", element: <Devices /> },
      { path: "audit", element: <Audit /> },
      { path: "*", element: <Navigate to="/" replace /> },
    ],
  },
]);
