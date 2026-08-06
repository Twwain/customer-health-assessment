import { BrowserRouter, Routes, Route, Navigate, useParams } from "react-router-dom";
import Layout from "./components/Layout";
import CustomerList from "./pages/CustomerList";
import Chat from "./pages/Chat";
import Knowledge from "./pages/Knowledge";

/** 会话路由按 sessionId 重挂载 Chat，保证切换会话时状态全新（无需手动 reset）。 */
function ChatBySession() {
  const { sessionId } = useParams();
  return <Chat key={sessionId ?? "new"} />;
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          {/* 评审结论 Q2：默认首页为客户库 */}
          <Route path="/" element={<Navigate to="/customers" replace />} />
          <Route path="/customers" element={<CustomerList />} />
          <Route path="/chat" element={<Chat />} />
          <Route path="/chat/:sessionId" element={<ChatBySession />} />
          <Route path="/knowledge" element={<Knowledge />} />
          <Route path="*" element={<Navigate to="/customers" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
