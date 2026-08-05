import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import Layout from "./components/Layout";
import CustomerList from "./pages/CustomerList";
import Chat from "./pages/Chat";
import Knowledge from "./pages/Knowledge";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          {/* 评审结论 Q2：默认首页为客户库 */}
          <Route path="/" element={<Navigate to="/customers" replace />} />
          <Route path="/customers" element={<CustomerList />} />
          <Route path="/chat" element={<Chat />} />
          <Route path="/chat/:sessionId" element={<Chat />} />
          <Route path="/knowledge" element={<Knowledge />} />
          <Route path="*" element={<Navigate to="/customers" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
