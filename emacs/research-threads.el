;;; research-threads.el --- Dashboard for AI research threads -*- lexical-binding: t -*-

;; Author: Max Sobol Mark
;; Keywords: tools, convenience
;; Package-Requires: ((emacs "29.1"))

;;; Commentary:

;; An Emacs-native dashboard over the research-threads server, which
;; auto-detects Claude Code / Codex sessions running in vterms.
;;
;;   M-x research-threads        open the dashboard (starts the server if
;;                               it isn't running)
;;   M-x research-threads-web    open the web dashboard in a browser
;;
;; In the dashboard buffer:
;;   n/p  move between threads
;;   C-n  start a new thread (asks name, agent, directory; opens a vterm)
;;   TAB  expand / collapse the thread at point (objective, status, last note)
;;   RET  jump to the thread's vterm (offers to reopen closed ones)
;;   c    add a note to the thread at point
;;   x    close the thread's vterm (kills its agent session)
;;   o    open the thread in the web dashboard
;;   a    archive / unarchive the thread at point
;;   *    pin / unpin the thread at point
;;   w    open the web dashboard
;;   g    refresh
;;   q    bury the dashboard
;;
;; The buffer refreshes itself while it is visible.

;;; Code:

(require 'json)
(require 'url)
(require 'subr-x)

(declare-function vterm "vterm")
(declare-function vterm-send-string "vterm")
(declare-function vterm-send-return "vterm")

(defgroup research-threads nil
  "Dashboard for AI research threads."
  :group 'tools)

(defcustom research-threads-port 7878
  "Port the research-threads server listens on."
  :type 'integer)

(defcustom research-threads-repo
  (expand-file-name ".." (file-name-directory (or load-file-name buffer-file-name)))
  "Directory containing server.py."
  :type 'directory)

(defcustom research-threads-refresh-interval 4
  "Seconds between automatic dashboard refreshes while visible."
  :type 'number)

(defconst research-threads--buffer "*research-threads*")
(defvar research-threads--timer nil)
(defvar research-threads--snapshot nil)

;;;; Faces — explicit colors per background so dark themes stay readable.
;;
;; The palette mirrors the web dashboard: warm ivory text, gold for working,
;; mint for ready, coral for needs-you, clay/teal for the agents.
;;
;; Plain `defface' is inert when the face already exists, which makes palette
;; tweaks silently no-op on reload. This variant forces the spec every load.

(defmacro research-threads--defface (name spec doc)
  "Like `defface', but re-applies SPEC on every load."
  (declare (indent defun))
  `(progn
     (defface ,name ,spec ,doc)
     (face-spec-set ',name ,spec 'face-defface-spec)))

(research-threads--defface research-threads-title
  '((((background dark)) :foreground "#f0eee6" :weight semi-bold :height 1.35)
    (t :foreground "#23221d" :weight semi-bold :height 1.35))
  "Face for the dashboard title.")

(research-threads--defface research-threads-section
  '((((background dark)) :foreground "#e6dec6" :slant italic :height 1.08)
    (t :foreground "#6f6b5d" :slant italic :height 1.08))
  "Face for section headings.")

(research-threads--defface research-threads-name
  '((((background dark)) :foreground "#ece9dd" :weight bold)
    (t :foreground "#23221d" :weight bold))
  "Face for thread names.")

(research-threads--defface research-threads-muted
  '((((background dark)) :foreground "#d8d2bf")
    (t :foreground "#6f6b5d"))
  "Face for secondary information: light warm parchment, never gray.")

(research-threads--defface research-threads-faint
  '((((background dark)) :foreground "#b3a170")
    (t :foreground "#a5a192"))
  "Face for decoration (rules, footers, ages): warm tan, never gray.")

(research-threads--defface research-threads-working
  '((((background dark)) :foreground "#e3b341")
    (t :foreground "#b8891c"))
  "Face for the working status.")

(research-threads--defface research-threads-ready
  '((((background dark)) :foreground "#57d9a0")
    (t :foreground "#4c9a70"))
  "Face for the ready status.")

(research-threads--defface research-threads-unread
  '((((background dark)) :foreground "#6aa9e9" :weight bold)
    (t :foreground "#3a73b0" :weight bold))
  "Face for a finished thread whose last message has not been read.")

(research-threads--defface research-threads-attention
  '((((background dark)) :foreground "#ff6a5c" :weight bold)
    (t :foreground "#c24e3c" :weight bold))
  "Face for statuses that need the user.")

(research-threads--defface research-threads-claude
  '((((background dark)) :foreground "#d98a6a")
    (t :foreground "#c15f3c"))
  "Face for the Claude agent tag.")

(research-threads--defface research-threads-codex
  '((((background dark)) :foreground "#4cc2b4")
    (t :foreground "#17877a"))
  "Face for the Codex agent tag.")

;;;; HTTP helpers

(defun research-threads--url (path)
  (format "http://localhost:%d%s" research-threads-port path))

(defun research-threads--get (path callback)
  "GET PATH asynchronously; call CALLBACK with parsed JSON or nil."
  (let ((url-request-method "GET"))
    (url-retrieve
     (research-threads--url path)
     (lambda (status)
       (let ((data
              (unless (plist-get status :error)
                (ignore-errors
                  (goto-char (point-min))
                  (re-search-forward "\n\n")
                  (json-parse-buffer :object-type 'alist :array-type 'list
                                     :null-object nil :false-object nil)))))
         (kill-buffer)
         (funcall callback data)))
     nil t t)))

(defun research-threads--post (path body callback)
  "POST BODY (an alist) as JSON to PATH; call CALLBACK with the response."
  (let ((url-request-method "POST")
        (url-request-extra-headers '(("Content-Type" . "application/json")))
        (url-request-data (encode-coding-string (json-serialize body) 'utf-8)))
    (url-retrieve
     (research-threads--url path)
     (lambda (_status)
       (let ((data (ignore-errors
                     (goto-char (point-min))
                     (re-search-forward "\n\n")
                     (json-parse-buffer :object-type 'alist :array-type 'list
                                        :null-object nil :false-object nil))))
         (kill-buffer)
         (when callback (funcall callback data))))
     nil t t)))

(defun research-threads--ensure-server (&optional then)
  "Start the server if it is not reachable, then call THEN."
  (research-threads--get
   "/api/health"
   (lambda (data)
     (if data
         (when then (funcall then))
       (let ((default-directory research-threads-repo))
         (start-process "research-threads-server" nil
                        "python3" (expand-file-name "server.py" research-threads-repo)))
       (when then (run-with-timer 1.5 nil then))))))

;;;; Rendering

(defconst research-threads--status-info
  '(("working"          "●" research-threads-working   "working")
    ("unread"           "◉" research-threads-unread    "new message")
    ("idle"             "●" research-threads-ready     "ready")
    ("needs-attention"  "◉" research-threads-attention "needs input")
    ("needs-permission" "◉" research-threads-attention "needs permission")
    ("closed"           "·" research-threads-muted     "closed")))

(defun research-threads--display-status (thread)
  "Status of THREAD as shown: idle plus an unseen reply reads as \"unread\"."
  (if (alist-get 'unread thread) "unread" (alist-get 'status thread)))

(defconst research-threads--bg-glyphs
  '((agents . "⚙") (monitors . "◷") (commands . "▸"))
  "Glyphs for in-flight subagents, monitors and background commands.")

(defun research-threads--bg-badges (thread)
  "Compact \"2⚙ 1◷\" summary of THREAD's in-flight background work."
  (let ((bg (alist-get 'background thread)))
    (if (not bg) ""
      (mapconcat
       #'identity
       (delq nil (mapcar (lambda (cell)
                           (let ((n (alist-get (car cell) bg)))
                             (and n (> n 0) (format "%d%s" n (cdr cell)))))
                         research-threads--bg-glyphs))
       " "))))

(defun research-threads--age (ts)
  (if (not ts) ""
    (let ((d (max 0 (- (float-time) ts))))
      (cond ((< d 60) "now")
            ((< d 3600) (format "%dm" (/ d 60)))
            ((< d 86400) (format "%dh" (/ d 3600)))
            ((< d (* 7 86400)) (format "%dd" (/ d 86400)))
            (t (format-time-string "%b %e" (seconds-to-time ts)))))))

(defvar research-threads--width 110
  "Window width the dashboard was last rendered for.")
(defvar research-threads--name-width 26
  "Computed width of the thread-name column for the current render.")

(defun research-threads--short-path (path limit)
  (if (not path) ""
    (let ((p (abbreviate-file-name path)))
      (if (<= (length p) limit) p
        (concat "…" (substring p (- (length p) (max 1 (1- limit)))))))))

(defun research-threads--agent-face (agent)
  (pcase agent
    ("claude" 'research-threads-claude)
    ("codex" 'research-threads-codex)
    (_ 'research-threads-muted)))

(defvar research-threads--expanded (make-hash-table :test 'eql)
  "Ids of threads whose detail block is currently expanded.")

(defun research-threads--insert-detail-line (bullet text face)
  "Insert an indented, filled detail line: BULLET then TEXT in FACE."
  (let ((start (point))
        (fill-column (max 40 (- research-threads--width 6)))
        (fill-prefix "        "))
    (insert (propertize (concat "      " bullet
                                (replace-regexp-in-string "[\n\r]+" " " text))
                        'face face))
    (fill-region start (point))
    (insert "\n")))

(defun research-threads--insert-thread (thread)
  "Insert one THREAD as a collapsed line, plus details when expanded."
  (let-alist thread
    (let* ((info (or (assoc (research-threads--display-status thread)
                            research-threads--status-info)
                     '("?" "·" research-threads-muted "?")))
           (glyph (nth 1 info))
           (face (nth 2 info))
           (word (nth 3 info))
           (expanded (gethash .id research-threads--expanded))
           (start (point)))
      (insert
       (propertize (format "  %s " (if expanded "▾" glyph)) 'face face)
       (propertize (format (format "%%-%ds" research-threads--name-width)
                           (truncate-string-to-width
                            (or .name "?") research-threads--name-width nil nil "…"))
                   'face 'research-threads-name)
       (propertize (format " %-7s" (or .agent "")) 'face (research-threads--agent-face .agent))
       (propertize (format " %-17s" word) 'face face)
       (propertize (format " %-8s" (research-threads--bg-badges thread))
                   'face 'research-threads-working)
       (propertize (format " %-5s" (research-threads--age .last_active_at))
                   'face 'research-threads-faint)
       (propertize (format " %s"
                           (research-threads--short-path
                            .cwd
                            (max 20 (- research-threads--width
                                       research-threads--name-width 48))))
                   'face 'research-threads-muted)
       "\n")
      (when expanded
        (when .objective
          (research-threads--insert-detail-line
           "◦ " .objective '(:inherit research-threads-muted :slant italic)))
        (when .status_text
          (research-threads--insert-detail-line
           "≡ "
           (concat .status_text
                   (if .status_updated_at
                       (format "  (updated %s)"
                               (research-threads--age .status_updated_at))
                     ""))
           'research-threads-muted))
        (when .latest_note
          (let-alist .latest_note
            (research-threads--insert-detail-line
             "└ "
             (format "%s%s" (if .author (format "%s · " .author) "") (or .text ""))
             'research-threads-muted)))
        (unless (or .objective .status_text .latest_note)
          (research-threads--insert-detail-line
           "" "no details yet" 'research-threads-faint)))
      (add-text-properties start (point) (list 'rt-thread thread)))))

(defun research-threads--insert-section (title threads)
  (when threads
    (let* ((count (number-to-string (length threads)))
           (rule-len (max 4 (- research-threads--width
                               (length title) (length count) 9))))
      (insert "\n"
              (propertize (format "  %s" title) 'face 'research-threads-section)
              (propertize (format "  %s " count) 'face 'research-threads-faint)
              (propertize (make-string rule-len ?─) 'face 'research-threads-faint)
              "\n"))
    (dolist (th threads)
      (research-threads--insert-thread th))))

(defun research-threads--render (snapshot)
  (setq research-threads--snapshot snapshot)
  (let ((buf (get-buffer research-threads--buffer)))
    (when (buffer-live-p buf)
      (with-current-buffer buf
        (let* ((inhibit-read-only t)
               (threads (alist-get 'threads snapshot))
               (win (get-buffer-window buf t))
               (research-threads--width
                (if win (window-width win) research-threads--width))
               ;; Name column: wide enough for the longest name when the
               ;; window allows it (leaving ~24 cols minimum for the path),
               ;; never narrower than 26.
               (research-threads--name-width
                (max 26 (min (apply #'max 10
                                    (mapcar (lambda (th)
                                              (length (or (alist-get 'name th) "")))
                                            threads))
                             (- research-threads--width 71))))
               (at-point (research-threads--thread-at-point))
               (saved-id (and at-point (alist-get 'id at-point)))
               (open (seq-filter (lambda (th) (alist-get 'open th)) threads))
               (needs (seq-filter
                       (lambda (th) (member (alist-get 'status th)
                                            '("needs-attention" "needs-permission")))
                       open))
               (working (seq-filter
                         (lambda (th) (equal (alist-get 'status th) "working")) open))
               (unread (seq-filter
                        (lambda (th) (equal (research-threads--display-status th) "unread"))
                        open))
               (ready (seq-filter
                       (lambda (th) (equal (research-threads--display-status th) "idle"))
                       open))
               (earlier (seq-filter
                         (lambda (th) (and (not (alist-get 'open th))
                                           (eq (alist-get 'archived th) 0)))
                         threads)))
          (erase-buffer)
          (insert "\n  " (propertize "Research Threads" 'face 'research-threads-title)
                  "   "
                  (propertize (format "%d open · %d waiting"
                                      (length open) (+ (length needs) (length unread)))
                              'face 'research-threads-muted)
                  "\n")
          (research-threads--insert-section "Needs you" needs)
          (research-threads--insert-section "New message" unread)
          (research-threads--insert-section "Working" working)
          (research-threads--insert-section "Ready" ready)
          (research-threads--insert-section "Earlier" earlier)
          (insert "\n"
                  (propertize
                   "  n/p move · C-n new · TAB details · RET jump · c note · x close · a archive · * pin · g refresh · q quit\n"
                   'face 'research-threads-faint))
          (goto-char (point-min))
          (unless (and saved-id (research-threads--goto-thread saved-id))
            (research-threads--goto-first-thread)))))))

(defun research-threads--goto-first-thread ()
  "Move point to the first thread line, if any."
  (let ((pos (if (get-text-property (point-min) 'rt-thread)
                 (point-min)
               (next-single-property-change (point-min) 'rt-thread))))
    (when pos
      (goto-char pos)
      (beginning-of-line))))

(defun research-threads--goto-thread (id)
  (let ((pos (point-min)) found)
    (while (and (not found)
                (setq pos (next-single-property-change pos 'rt-thread)))
      (let ((th (get-text-property pos 'rt-thread)))
        (when (and th (equal (alist-get 'id th) id))
          (goto-char pos)
          (beginning-of-line)
          (setq found t))))
    found))

(defun research-threads--refresh (&optional interactive)
  "Fetch the latest snapshot and re-render."
  (interactive "p")
  (research-threads--get
   "/api/state"
   (lambda (data)
     (cond (data (research-threads--render data))
           (interactive
            (message "research-threads: server unreachable, starting it…")
            (research-threads--ensure-server #'research-threads--refresh))))))

;;;; Commands on the thread at point

(defun research-threads--thread-at-point ()
  (get-text-property (point) 'rt-thread))

(defun research-threads--require-thread ()
  (or (research-threads--thread-at-point)
      (user-error "No thread at point")))

(defun research-threads--id-at (pos)
  (let ((th (get-text-property pos 'rt-thread)))
    (and th (alist-get 'id th))))

(defun research-threads--move (dir)
  "Move point to the first line of the next thread in direction DIR."
  (let ((origin (point))
        (cur (research-threads--id-at (point)))
        target)
    (save-excursion
      (while (and (not target) (zerop (forward-line dir)))
        (let ((id (research-threads--id-at (point))))
          (when (and id (not (eql id cur)))
            ;; Walk back to this thread's first line.
            (while (and (zerop (forward-line -1))
                        (eql (research-threads--id-at (point)) id)))
            (unless (eql (research-threads--id-at (point)) id)
              (forward-line 1))
            (setq target (point))))))
    (if target
        (goto-char target)
      (goto-char origin)
      (message "No more threads"))))

(defun research-threads-next ()
  "Move to the next thread."
  (interactive)
  (research-threads--move 1))

(defun research-threads-previous ()
  "Move to the previous thread."
  (interactive)
  (research-threads--move -1))

(defun research-threads-toggle-expand ()
  "Expand or collapse the details of the thread at point."
  (interactive)
  (let ((id (alist-get 'id (research-threads--require-thread))))
    (if (gethash id research-threads--expanded)
        (remhash id research-threads--expanded)
      (puthash id t research-threads--expanded))
    (when research-threads--snapshot
      (research-threads--render research-threads--snapshot))))

(defun research-threads-visit ()
  "Jump to the vterm of the thread at point, reopening it if needed."
  (interactive)
  (let-alist (research-threads--require-thread)
    (when .unread                       ; looking at the vterm counts as reading
      (research-threads--post (format "/api/threads/%s/mark_read" .id) '() nil))
    (let* ((vterm-name (and (string-prefix-p "vterm:" .key)
                            (substring .key (length "vterm:"))))
           (buf-name (and vterm-name (format "*vterm-%s*" vterm-name))))
      (cond
       ((and buf-name (get-buffer buf-name))
        (switch-to-buffer (get-buffer buf-name)))
       ((and buf-name (fboundp 'vterm)
             (y-or-n-p (format "Vterm %s is closed — open a new one%s? "
                               vterm-name
                               (if .cwd (format " in %s" (abbreviate-file-name .cwd)) ""))))
        (let ((default-directory (or (and .cwd (file-directory-p .cwd) .cwd) "~/"))
              (process-environment
               (cons (format "CLAUDE_VTERM_NAME=%s" vterm-name) process-environment)))
          (vterm buf-name)))
       (t (message "No vterm associated with this thread"))))))

(defun research-threads-note ()
  "Add a note to the thread at point."
  (interactive)
  (let* ((thread (research-threads--require-thread))
         (id (alist-get 'id thread))
         (name (alist-get 'name thread))
         (text (read-string (format "Note for %s: " name))))
    (when (string-empty-p (string-trim text))
      (user-error "Empty note"))
    (research-threads--post
     "/api/notes" `((thread_id . ,id) (text . ,text) (author . "max"))
     (lambda (_) (message "Noted → %s" name) (research-threads--refresh)))))

(defun research-threads-toggle-archive ()
  "Archive or unarchive the thread at point."
  (interactive)
  (let-alist (research-threads--require-thread)
    (research-threads--post
     (format "/api/threads/%d/%s" .id (if (eq .archived 0) "archive" "unarchive"))
     '() (lambda (_) (research-threads--refresh)))))

(defun research-threads-toggle-pin ()
  "Pin or unpin the thread at point."
  (interactive)
  (let-alist (research-threads--require-thread)
    (research-threads--post
     (format "/api/threads/%d/%s" .id (if (eq .pinned 0) "pin" "unpin"))
     '() (lambda (_) (research-threads--refresh)))))

(defun research-threads-web-thread ()
  "Open the thread at point in the web dashboard."
  (interactive)
  (let-alist (research-threads--require-thread)
    (browse-url (research-threads--url (format "/#t%d" .id)))))

(defun research-threads-close-vterm ()
  "Kill the vterm buffer of the thread at point (ends its agent session)."
  (interactive)
  (let-alist (research-threads--require-thread)
    (let* ((vterm-name (and (string-prefix-p "vterm:" .key)
                            (substring .key (length "vterm:"))))
           (buf (and vterm-name (get-buffer (format "*vterm-%s*" vterm-name)))))
      (cond
       ((null buf)
        (message "No open vterm for %s" .name))
       ((y-or-n-p (format "Close vterm for %s (kills its session)? " .name))
        (when-let ((proc (get-buffer-process buf)))
          (set-process-query-on-exit-flag proc nil))
        (kill-buffer buf)
        (message "Closed %s" (buffer-name buf))
        ;; The server notices the process is gone within a couple of polls;
        ;; refresh shortly after so the thread moves to Earlier.
        (run-with-timer 6 nil #'research-threads--refresh))))))

(defun research-threads-new ()
  "Start a new research thread: prompt for name, agent and directory,
register the thread, then open a vterm there running the chosen agent."
  (interactive)
  (unless (fboundp 'vterm)
    (user-error "vterm is not available"))
  (let* ((name (string-trim (read-string "Thread name: ")))
         (_ (when (string-empty-p name) (user-error "Thread name required")))
         (buf-name (format "*vterm-%s*" name))
         (_ (when (get-buffer buf-name)
              (user-error "A vterm named %s already exists" buf-name)))
         (agent (completing-read "Agent: " '("claude" "codex") nil t nil nil "claude"))
         (dir (read-directory-name "Directory: " "~/dev/" nil t))
         (objective (string-trim (read-string "Objective (optional): ")))
         (cwd (directory-file-name (expand-file-name dir))))
    (research-threads--post
     "/api/register"
     `((name . ,name) (vterm . ,name) (cwd . ,cwd) (author . ,agent)
       ,@(unless (string-empty-p objective) `((objective . ,objective))))
     (lambda (_resp) (research-threads--refresh)))
    (let ((default-directory (file-name-as-directory cwd))
          (process-environment
           (cons (format "CLAUDE_VTERM_NAME=%s" name) process-environment)))
      (vterm buf-name))
    (vterm-send-string agent)
    (vterm-send-return)))

;;;###autoload
(defun research-threads-web ()
  "Open the web dashboard."
  (interactive)
  (research-threads--ensure-server
   (lambda () (browse-url (research-threads--url "/")))))

;;;; Mode

(defvar research-threads-mode-map (make-sparse-keymap)
  "Keymap for `research-threads-mode'.")

;; Bindings are applied on every load (mutating the same map object), so
;; reloading the file updates keys in already-open dashboard buffers too.
(set-keymap-parent research-threads-mode-map special-mode-map)
(dolist (binding '(("RET"   research-threads-visit)
                   ("TAB"   research-threads-toggle-expand)
                   ("<tab>" research-threads-toggle-expand)
                   ("n"     research-threads-next)
                   ("p"     research-threads-previous)
                   ("C-n"   research-threads-new)
                   ("c"     research-threads-note)
                   ("o"     research-threads-web-thread)
                   ("x"     research-threads-close-vterm)
                   ("a"     research-threads-toggle-archive)
                   ("*"     research-threads-toggle-pin)
                   ("w"     research-threads-web)
                   ("g"     research-threads--refresh)))
  (define-key research-threads-mode-map (kbd (car binding)) (cadr binding)))

(define-derived-mode research-threads-mode special-mode "Research-Threads"
  "Major mode for the research threads dashboard."
  (setq-local cursor-type nil)
  (setq-local truncate-lines t)
  (setq-local line-spacing 0.18)
  (hl-line-mode 1))

(defvar research-threads--last-width nil)

(defun research-threads--on-window-change (_frame)
  "Re-render when the dashboard window's width changes."
  (let* ((buf (get-buffer research-threads--buffer))
         (win (and (buffer-live-p buf) (get-buffer-window buf t))))
    (when (and win research-threads--snapshot)
      (let ((w (window-width win)))
        (unless (eql w research-threads--last-width)
          (setq research-threads--last-width w)
          (research-threads--render research-threads--snapshot))))))

(add-hook 'window-size-change-functions #'research-threads--on-window-change)

(defun research-threads--start-timer ()
  (when research-threads--timer
    (cancel-timer research-threads--timer))
  (setq research-threads--timer
        (run-with-timer
         research-threads-refresh-interval research-threads-refresh-interval
         (lambda ()
           (let ((buf (get-buffer research-threads--buffer)))
             (if (not (buffer-live-p buf))
                 (when research-threads--timer
                   (cancel-timer research-threads--timer)
                   (setq research-threads--timer nil))
               (when (get-buffer-window buf t)
                 (research-threads--refresh))))))))

;;;###autoload
(defun research-threads ()
  "Open the research threads dashboard."
  (interactive)
  (let ((buf (get-buffer-create research-threads--buffer)))
    (with-current-buffer buf
      ;; Re-enable the mode every time so a reloaded definition takes over.
      (research-threads-mode)
      (when (zerop (buffer-size))
        (let ((inhibit-read-only t))
          (insert "\n  " (propertize "Research Threads" 'face 'research-threads-title)
                  "\n\n  " (propertize "connecting…" 'face 'research-threads-muted) "\n"))))
    (switch-to-buffer buf)
    (unless (bound-and-true-p server-process)
      (ignore-errors (server-start)))    ; lets the web app focus Emacs buffers
    (research-threads--ensure-server #'research-threads--refresh)
    (research-threads--start-timer)))

(provide 'research-threads)
;;; research-threads.el ends here
