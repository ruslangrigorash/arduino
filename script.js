class QuickNotesApp {
    constructor() {
        this.notes = this.loadFromStorage('notes') || [];
        this.tasks = this.loadFromStorage('tasks') || [];
        this.currentTheme = this.loadFromStorage('theme') || 'light';
        this.init();
    }

    init() {
        this.setupTheme();
        this.setupTabs();
        this.setupEventListeners();
        this.render();
    }

    setupTheme() {
        document.documentElement.setAttribute('data-theme', this.currentTheme);
        const themeToggle = document.getElementById('themeToggle');
        themeToggle.textContent = this.currentTheme === 'dark' ? '☀️' : '🌙';
    }

    setupTabs() {
        const tabBtns = document.querySelectorAll('.tab-btn');
        tabBtns.forEach(btn => {
            btn.addEventListener('click', () => {
                tabBtns.forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                
                const targetTab = btn.dataset.tab;
                document.querySelectorAll('.tab-content').forEach(content => {
                    content.classList.remove('active');
                });
                document.getElementById(`${targetTab}-tab`).classList.add('active');
            });
        });
    }

    setupEventListeners() {
        document.getElementById('themeToggle').addEventListener('click', () => this.toggleTheme());
        document.getElementById('addNote').addEventListener('click', () => this.addNote());
        document.getElementById('addTask').addEventListener('click', () => this.addTask());
        
        document.getElementById('noteInput').addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && e.metaKey) {
                this.addNote();
            }
        });
        
        document.getElementById('taskInput').addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                this.addTask();
            }
        });
    }

    toggleTheme() {
        this.currentTheme = this.currentTheme === 'light' ? 'dark' : 'light';
        this.setupTheme();
        this.saveToStorage('theme', this.currentTheme);
    }

    addNote() {
        const input = document.getElementById('noteInput');
        const content = input.value.trim();
        
        if (!content) {
            this.shakeElement(input);
            return;
        }

        const note = {
            id: Date.now(),
            content: content,
            timestamp: new Date().toISOString()
        };

        this.notes.unshift(note);
        this.saveToStorage('notes', this.notes);
        input.value = '';
        this.render();
        this.animateAdd(input);
    }

    addTask() {
        const input = document.getElementById('taskInput');
        const content = input.value.trim();
        
        if (!content) {
            this.shakeElement(input);
            return;
        }

        const task = {
            id: Date.now(),
            content: content,
            completed: false,
            timestamp: new Date().toISOString()
        };

        this.tasks.unshift(task);
        this.saveToStorage('tasks', this.tasks);
        input.value = '';
        this.render();
        this.animateAdd(input);
    }

    deleteNote(id) {
        this.notes = this.notes.filter(note => note.id !== id);
        this.saveToStorage('notes', this.notes);
        this.render();
    }

    deleteTask(id) {
        this.tasks = this.tasks.filter(task => task.id !== id);
        this.saveToStorage('tasks', this.tasks);
        this.render();
    }

    toggleTask(id) {
        const task = this.tasks.find(t => t.id === id);
        if (task) {
            task.completed = !task.completed;
            this.saveToStorage('tasks', this.tasks);
            this.render();
        }
    }

    render() {
        this.renderNotes();
        this.renderTasks();
        this.updateStats();
    }

    renderNotes() {
        const container = document.getElementById('notesList');
        
        if (this.notes.length === 0) {
            container.innerHTML = `
                <div class="empty-state">
                    <div class="empty-state-icon">📝</div>
                    <div class="empty-state-text">No notes yet. Create your first note!</div>
                </div>
            `;
            return;
        }

        container.innerHTML = this.notes.map(note => `
            <div class="note-item">
                <div class="note-content">${this.escapeHtml(note.content)}</div>
                <div class="item-footer">
                    <span class="item-time">${this.formatTime(note.timestamp)}</span>
                    <button class="delete-btn" onclick="app.deleteNote(${note.id})">Delete</button>
                </div>
            </div>
        `).join('');
    }

    renderTasks() {
        const container = document.getElementById('tasksList');
        
        if (this.tasks.length === 0) {
            container.innerHTML = `
                <div class="empty-state">
                    <div class="empty-state-icon">✅</div>
                    <div class="empty-state-text">No tasks yet. Add your first task!</div>
                </div>
            `;
            return;
        }

        container.innerHTML = this.tasks.map(task => `
            <div class="task-item ${task.completed ? 'completed' : ''}">
                <input 
                    type="checkbox" 
                    class="task-checkbox" 
                    ${task.completed ? 'checked' : ''}
                    onchange="app.toggleTask(${task.id})"
                >
                <div style="flex: 1;">
                    <div class="task-content">${this.escapeHtml(task.content)}</div>
                    <div class="item-footer">
                        <span class="item-time">${this.formatTime(task.timestamp)}</span>
                        <button class="delete-btn" onclick="app.deleteTask(${task.id})">Delete</button>
                    </div>
                </div>
            </div>
        `).join('');
    }

    updateStats() {
        const noteCount = document.getElementById('noteCount');
        const taskCount = document.getElementById('taskCount');
        const completedTasks = this.tasks.filter(t => t.completed).length;
        
        noteCount.textContent = `${this.notes.length} note${this.notes.length !== 1 ? 's' : ''}`;
        taskCount.textContent = `${completedTasks}/${this.tasks.length} task${this.tasks.length !== 1 ? 's' : ''}`;
    }

    formatTime(timestamp) {
        const date = new Date(timestamp);
        const now = new Date();
        const diff = now - date;
        const minutes = Math.floor(diff / 60000);
        const hours = Math.floor(minutes / 60);
        const days = Math.floor(hours / 24);

        if (minutes < 1) return 'Just now';
        if (minutes < 60) return `${minutes}m ago`;
        if (hours < 24) return `${hours}h ago`;
        if (days < 7) return `${days}d ago`;
        
        return date.toLocaleDateString();
    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    shakeElement(element) {
        element.style.animation = 'none';
        setTimeout(() => {
            element.style.animation = 'shake 0.3s ease';
        }, 10);
    }

    animateAdd(element) {
        element.style.transform = 'scale(0.95)';
        setTimeout(() => {
            element.style.transform = 'scale(1)';
        }, 100);
    }

    loadFromStorage(key) {
        try {
            const data = localStorage.getItem(`quicknotes_${key}`);
            return data ? JSON.parse(data) : null;
        } catch (e) {
            console.error('Error loading from storage:', e);
            return null;
        }
    }

    saveToStorage(key, value) {
        try {
            localStorage.setItem(`quicknotes_${key}`, JSON.stringify(value));
        } catch (e) {
            console.error('Error saving to storage:', e);
        }
    }
}

const style = document.createElement('style');
style.textContent = `
    @keyframes shake {
        0%, 100% { transform: translateX(0); }
        25% { transform: translateX(-10px); }
        75% { transform: translateX(10px); }
    }
`;
document.head.appendChild(style);

const app = new QuickNotesApp();
