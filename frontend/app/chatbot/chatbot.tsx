"use client";

import { useState } from "react";
import "./chatbot.css";
import Waves from "../../src/component/Waves";
import { queryAgent } from "../../src/lib/api";

interface Message {
  role: "bot" | "user";
  content: string;
}

export default function Chatbot() {
  const [messages, setMessages] = useState<Message[]>([
    {
      role: "bot",
      content: "Welcome to Miax - how can I assist you today?",
    },
  ]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);

  async function handleSend(e: React.FormEvent) {
    e.preventDefault();
    if (!input.trim() || loading) return;

    const userMsg: Message = { role: "user", content: input };
    setMessages((prev) => [...prev, userMsg]);
    setInput("");
    setLoading(true);

    try {
      const response = await queryAgent({
        username: "user@miax.com", // TODO: replace with real user from auth
        permission_group: "permissions_group_a", // TODO: replace with user's actual group
        prompt: input,
      });

      const botMsg: Message = { role: "bot", content: response.answer };
      setMessages((prev) => [...prev, botMsg]);
    } catch (err) {
      const errorMsg: Message = {
        role: "bot",
        content: `Sorry, something went wrong: ${err instanceof Error ? err.message : "Unknown error"}`,
      };
      setMessages((prev) => [...prev, errorMsg]);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="chatbot-page">
      {/* Background wave */}
      <div className="chatbot-wave-container">
        <Waves
          lineColor="rgba(200, 130, 50, 0.25)"
          backgroundColor="transparent"
          waveSpeedX={0.02}
          waveSpeedY={0.01}
          waveAmpX={40}
          waveAmpY={20}
          friction={0.9}
          tension={0.01}
          maxCursorMove={120}
          xGap={12}
          yGap={36}
        />
      </div>

      {/* Decorative star */}
      <div className="chatbot-star">&#10022;</div>

      {/* Chat container */}
      <div className="chat-container">
        <div className="chat-header">
          <span className="chat-logo"><strong>MIAX</strong> CHAT</span>
        </div>

        <div className="chat-hero">
          <h1>Your Retrieval Assistant</h1>
        </div>

        <div className="chat-messages">
          {messages.map((msg, i) => (
            <div key={i} className={`chat-bubble ${msg.role}`}>
              <p>{msg.content}</p>
            </div>
          ))}
        </div>

        <form className="chat-input-area" onSubmit={handleSend}>
          <div className="chat-input-wrapper">
            <input
              type="text"
              placeholder="Type your market query here..."
              value={input}
              onChange={(e) => setInput(e.target.value)}
              className="chat-input"
            />
            <div className="chat-input-actions">
              <button type="button" className="input-icon-btn" aria-label="Copy">&#9112;</button>
              <button type="button" className="input-icon-btn" aria-label="Attach">&#128206;</button>
            </div>
            <button type="submit" className="chat-send-btn">
              ASK MIAX &rarr;
            </button>
            <button type="button" className="chat-settings-btn" aria-label="Settings">&#9881;</button>
          </div>
        </form>
      </div>
    </div>
  );
}
