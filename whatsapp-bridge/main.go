// Local, per-account WhatsApp Web companion bridge for Autopilot.
// This is an experimental unofficial transport; it deliberately stores only
// whatsmeow device credentials and does not persist chat history.
package main

import (
	"bytes"
	"context"
	"crypto/subtle"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	_ "github.com/mattn/go-sqlite3"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

const maxRequestBytes = 64 * 1024

type config struct {
	address      string
	dataDir      string
	apiToken     string
	callbackURL  string
	callbackToken string
}

type session struct {
	mu       sync.RWMutex
	client   *whatsmeow.Client
	store    *sqlstore.Container
	qr       string
	status   string
	error    string
	closeQR  context.CancelFunc
}

type manager struct {
	mu       sync.RWMutex
	items    map[string]*session
	config   config
	client   *http.Client
}

type qrStartRequest struct { ChannelID string `json:"channel_id"` }
type sendRequest struct {
	ChannelID string `json:"channel_id"`
	To        string `json:"to"`
	Text      string `json:"text"`
	MessageID string `json:"message_id"`
}
type inboundEvent struct {
	ChannelID string `json:"channel_id"`
	EventID   string `json:"event_id"`
	ChatID    string `json:"chat_id"`
	SenderID  string `json:"sender_id"`
	SenderName string `json:"sender_name"`
	Text      string `json:"text"`
}

func main() {
	cfg := config{
		address:       env("WHATSAPP_BRIDGE_ADDR", "127.0.0.1:8092"),
		dataDir:       env("WHATSAPP_BRIDGE_DATA_DIR", "./data/whatsapp-personal"),
		apiToken:      strings.TrimSpace(os.Getenv("WHATSAPP_BRIDGE_TOKEN")),
		callbackURL:   strings.TrimSpace(os.Getenv("WHATSAPP_BRIDGE_CALLBACK_URL")),
		callbackToken: strings.TrimSpace(os.Getenv("WHATSAPP_BRIDGE_CALLBACK_TOKEN")),
	}
	if cfg.apiToken == "" || cfg.callbackToken == "" || cfg.callbackURL == "" {
		log.Fatal("WHATSAPP_BRIDGE_TOKEN, WHATSAPP_BRIDGE_CALLBACK_TOKEN and WHATSAPP_BRIDGE_CALLBACK_URL are required")
	}
	if err := os.MkdirAll(cfg.dataDir, 0700); err != nil { log.Fatal(err) }
	if err := os.Chmod(cfg.dataDir, 0700); err != nil { log.Fatal(err) }
	m := &manager{items: make(map[string]*session), config: cfg, client: &http.Client{Timeout: 8 * time.Second}}
	if err := m.restoreSessions(); err != nil { log.Fatal(err) }
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, _ *http.Request) { writeJSON(w, http.StatusOK, map[string]any{"ok": true}) })
	mux.Handle("POST /v1/sessions/qr/start", m.auth(http.HandlerFunc(m.startQR)))
	mux.Handle("GET /v1/sessions/{channelID}/status", m.auth(http.HandlerFunc(m.getStatus)))
	mux.Handle("POST /v1/sessions/{channelID}/stop", m.auth(http.HandlerFunc(m.stop)))
	mux.Handle("POST /v1/messages/send", m.auth(http.HandlerFunc(m.send)))
	server := &http.Server{Addr: cfg.address, Handler: mux, ReadHeaderTimeout: 3 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 10 * time.Second, IdleTimeout: 30 * time.Second}
	ln, err := net.Listen("tcp", cfg.address)
	if err != nil { log.Fatal(err) }
	if host, _, _ := net.SplitHostPort(ln.Addr().String()); host != "127.0.0.1" && host != "localhost" && host != "::1" {
		log.Fatal("WhatsApp personal bridge must bind to loopback only")
	}
	log.Printf("WhatsApp personal bridge listening on %s", ln.Addr())
	log.Fatal(server.Serve(ln))
}

func (m *manager) restoreSessions() error {
	paths, err := filepath.Glob(filepath.Join(m.config.dataDir, "*.db"))
	if err != nil { return err }
	for _, path := range paths {
		id := strings.TrimSuffix(filepath.Base(path), ".db")
		if !validID(id) { continue }
		store, err := sqlstore.New(context.Background(), "sqlite3", "file:"+filepath.ToSlash(path)+"?_foreign_keys=on", waLog.Noop)
		if err != nil { return fmt.Errorf("open WhatsApp session %s: %w", id, err) }
		if err := os.Chmod(path, 0600); err != nil { _ = store.Close(); return fmt.Errorf("protect WhatsApp session %s: %w", id, err) }
		device, err := store.GetFirstDevice(context.Background())
		if err != nil { _ = store.Close(); return fmt.Errorf("load WhatsApp session %s: %w", id, err) }
		if device.ID == nil { _ = store.Close(); continue }
		client := whatsmeow.NewClient(device, waLog.Noop)
		s := &session{client: client, store: store, status: "connecting"}
		client.AddEventHandler(func(evt any) { m.handleEvent(id, s, evt) })
		m.items[id] = s
		if err := client.Connect(); err != nil { s.mu.Lock(); s.status = "error"; s.error = "reconnect failed"; s.mu.Unlock(); log.Printf("WhatsApp reconnect failed channel=%s: %v", id, err); continue }
		s.mu.Lock(); s.status = "active"; s.mu.Unlock()
	}
	return nil
}

func (m *manager) auth(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		auth := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
		if subtle.ConstantTimeCompare([]byte(auth), []byte(m.config.apiToken)) != 1 { http.Error(w, "unauthorized", http.StatusUnauthorized); return }
		r.Body = http.MaxBytesReader(w, r.Body, maxRequestBytes)
		next.ServeHTTP(w, r)
	})
}

func (m *manager) startQR(w http.ResponseWriter, r *http.Request) {
	var body qrStartRequest
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || !validID(body.ChannelID) { http.Error(w, "invalid channel_id", http.StatusBadRequest); return }
	m.mu.Lock()
	if current := m.items[body.ChannelID]; current != nil {
		current.mu.RLock(); state := current.status; current.mu.RUnlock()
		if state == "waiting" || state == "active" { m.mu.Unlock(); writeSession(w, body.ChannelID, current); return }
		m.closeSession(body.ChannelID)
	}
	if len(m.items) >= 20 { m.mu.Unlock(); http.Error(w, "bridge session limit reached", http.StatusTooManyRequests); return }
	ctx := r.Context()
	dbPath := filepath.Join(m.config.dataDir, body.ChannelID+".db")
	store, err := sqlstore.New(ctx, "sqlite3", "file:"+filepath.ToSlash(dbPath)+"?_foreign_keys=on", waLog.Noop)
	if err != nil { m.mu.Unlock(); http.Error(w, "could not open session store", http.StatusInternalServerError); return }
	if err := os.Chmod(dbPath, 0600); err != nil { _ = store.Close(); m.mu.Unlock(); http.Error(w, "could not protect session store", http.StatusInternalServerError); return }
	device, err := store.GetFirstDevice(ctx)
	if err != nil { _ = store.Close(); m.mu.Unlock(); http.Error(w, "could not initialize session store", http.StatusInternalServerError); return }
	if device.ID != nil {
		client := whatsmeow.NewClient(device, waLog.Noop)
		s := &session{client: client, store: store, status: "connecting"}
		client.AddEventHandler(func(evt any) { m.handleEvent(body.ChannelID, s, evt) })
		m.items[body.ChannelID] = s
		m.mu.Unlock()
		if err := client.Connect(); err != nil { s.mu.Lock(); s.status = "error"; s.error = "reconnect failed"; s.mu.Unlock(); writeSession(w, body.ChannelID, s); return }
		s.mu.Lock(); s.status = "active"; s.mu.Unlock()
		writeSession(w, body.ChannelID, s)
		return
	}
	qrCtx, cancel := context.WithCancel(context.Background())
	client := whatsmeow.NewClient(device, waLog.Noop)
	s := &session{client: client, store: store, status: "waiting", closeQR: cancel}
	qrChan, err := client.GetQRChannel(qrCtx)
	if err != nil { cancel(); _ = store.Close(); m.mu.Unlock(); http.Error(w, "could not start QR pairing", http.StatusBadGateway); return }
	client.AddEventHandler(func(evt any) { m.handleEvent(body.ChannelID, s, evt) })
	m.items[body.ChannelID] = s
	if err := client.Connect(); err != nil { delete(m.items, body.ChannelID); m.mu.Unlock(); _ = store.Close(); http.Error(w, "could not connect to WhatsApp", http.StatusBadGateway); return }
	m.mu.Unlock()
	go func() {
		for item := range qrChan {
			s.mu.Lock()
			switch item.Event {
			case whatsmeow.QRChannelEventCode: s.qr = item.Code; s.status = "waiting"
			case "success": s.qr = ""; s.status = "active"
			case "timeout": s.qr = ""; if s.status != "active" { s.status = "expired" }
			case whatsmeow.QRChannelEventError: s.qr = ""; s.status = "error"; s.error = "QR pairing failed"
			case "err-client-outdated": s.qr = ""; s.status = "error"; s.error = "WhatsApp Web client needs update"
			}
			s.mu.Unlock()
		}
	}()
	writeSession(w, body.ChannelID, s)
}

func (m *manager) getStatus(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("channelID")
	m.mu.RLock(); s := m.items[id]; m.mu.RUnlock()
	if s == nil { writeJSON(w, http.StatusOK, map[string]any{"channel_id": id, "status": "disconnected"}); return }
	writeSession(w, id, s)
}

func (m *manager) stop(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("channelID")
	m.mu.Lock()
	s := m.items[id]
	if s != nil { delete(m.items, id) }
	m.mu.Unlock()
	if s == nil { http.Error(w, "session not found", http.StatusNotFound); return }
	s.mu.Lock()
	if s.closeQR != nil { s.closeQR() }
	s.status = "disconnected"
	s.qr = ""
	s.mu.Unlock()
	ctx, cancel := context.WithTimeout(r.Context(), 8*time.Second)
	logoutErr := s.client.Logout(ctx)
	if logoutErr != nil {
		// Logout keeps local credentials when the remote unlink fails. Clear them
		// explicitly so restoreSessions cannot silently sign this account back in.
		s.client.Disconnect()
		if err := s.client.Store.Delete(ctx); err != nil {
			log.Printf("WhatsApp local device-store deletion failed channel=%s: %v", id, err)
		}
	}
	cancel()
	if err := s.store.Close(); err != nil { log.Printf("WhatsApp session DB close failed channel=%s: %v", id, err) }
	for _, suffix := range []string{".db", ".db-wal", ".db-shm"} {
		if err := os.Remove(filepath.Join(m.config.dataDir, id+suffix)); err != nil && !os.IsNotExist(err) {
			log.Printf("WhatsApp session file removal failed channel=%s file=%s: %v", id, suffix, err)
		}
	}
	result := map[string]any{"channel_id": id, "status": "disconnected", "remote_unlinked": logoutErr == nil}
	if logoutErr != nil { result["warning"] = "Remote unlink did not complete; local session was removed. Check linked devices in WhatsApp." }
	writeJSON(w, http.StatusOK, result)
}

func (m *manager) closeSession(id string) bool {
	s := m.items[id]
	if s == nil { return false }
	delete(m.items, id)
	s.mu.Lock(); if s.closeQR != nil { s.closeQR() }; s.status = "disconnected"; s.qr = ""; s.mu.Unlock()
	s.client.Disconnect()
	_ = s.store.Close()
	return true
}

func (m *manager) send(w http.ResponseWriter, r *http.Request) {
	var body sendRequest
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || !validID(body.ChannelID) || strings.TrimSpace(body.To) == "" || strings.TrimSpace(body.Text) == "" || len(body.Text) > 4000 {
		http.Error(w, "invalid message payload", http.StatusBadRequest); return
	}
	m.mu.RLock(); s := m.items[body.ChannelID]; m.mu.RUnlock()
	if s == nil { http.Error(w, "session is not active", http.StatusConflict); return }
	s.mu.RLock(); active := s.status == "active"; client := s.client; s.mu.RUnlock()
	if !active { http.Error(w, "session is not active", http.StatusConflict); return }
	jid, err := types.ParseJID(body.To)
	if err != nil || (jid.Server != types.DefaultUserServer && jid.Server != types.HiddenUserServer && jid.Server != types.LegacyUserServer) { http.Error(w, "recipient must be a one-to-one WhatsApp JID", http.StatusBadRequest); return }
	resp, err := client.SendMessage(r.Context(), jid, &waE2E.Message{Conversation: proto.String(body.Text)})
	if err != nil { http.Error(w, "WhatsApp send failed", http.StatusBadGateway); return }
	writeJSON(w, http.StatusOK, map[string]any{"delivered": true, "message_id": resp.ID, "status": "sent", "provider": "whatsmeow"})
}

func (m *manager) handleEvent(channelID string, s *session, raw any) {
	msg, ok := raw.(*events.Message)
	if !ok { if _, lost := raw.(*events.LoggedOut); lost { s.mu.Lock(); s.status = "disconnected"; s.qr = ""; s.mu.Unlock() }; return }
	if msg.Info.IsFromMe || msg.Info.IsGroup || msg.Info.Chat.Server == types.BroadcastServer || msg.Info.IsNewsletterStatus { return }
	text := strings.TrimSpace(msg.Message.GetConversation())
	if text == "" && msg.Message.GetExtendedTextMessage() != nil { text = strings.TrimSpace(msg.Message.GetExtendedTextMessage().GetText()) }
	if text == "" { return }
	chatID := msg.Info.Chat.String()
	event := inboundEvent{ChannelID: channelID, EventID: chatID+":"+msg.Info.ID, ChatID: chatID, SenderID: msg.Info.Sender.String(), SenderName: strings.TrimSpace(msg.Info.PushName), Text: text}
	body, err := json.Marshal(event)
	if err != nil { return }
	for attempt := 0; attempt < 3; attempt++ {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, m.config.callbackURL, bytes.NewReader(body))
		if err != nil { cancel(); return }
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Authorization", "Bearer "+m.config.callbackToken)
		resp, err := m.client.Do(req)
		if err == nil {
			_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1024))
			_ = resp.Body.Close()
			if resp.StatusCode < 300 { cancel(); return }
			log.Printf("WhatsApp inbound callback rejected channel=%s status=%d attempt=%d", channelID, resp.StatusCode, attempt+1)
		} else {
			log.Printf("WhatsApp inbound callback failed channel=%s attempt=%d: %v", channelID, attempt+1, err)
		}
		cancel()
		if attempt < 2 { time.Sleep(time.Duration(attempt+1) * 300 * time.Millisecond) }
	}
}

func writeSession(w http.ResponseWriter, id string, s *session) {
	s.mu.RLock(); defer s.mu.RUnlock()
	accountID := ""
	if s.client.Store.ID != nil { accountID = s.client.Store.ID.String() }
	writeJSON(w, http.StatusOK, map[string]any{"channel_id": id, "status": s.status, "qr": s.qr, "error": s.error, "account_id": accountID})
}
func writeJSON(w http.ResponseWriter, code int, value any) { w.Header().Set("Content-Type", "application/json"); w.WriteHeader(code); _ = json.NewEncoder(w).Encode(value) }
func validID(id string) bool {
	if len(id) != 36 { return false }
	for i, c := range id { if i == 8 || i == 13 || i == 18 || i == 23 { if c != '-' { return false }; continue }; if !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F')) { return false } }
	return true
}
func env(key, fallback string) string { if value := strings.TrimSpace(os.Getenv(key)); value != "" { return value }; return fallback }
