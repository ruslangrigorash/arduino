# QuickNotes - Mobile Web App 📝

A beautiful, lightweight notes and tasks app that works perfectly on iPhone and any mobile device!

## Features ✨

- **Notes**: Write and save quick notes
- **Tasks**: Create and manage your to-do list with checkboxes
- **Dark Mode**: Toggle between light and dark themes
- **Offline Storage**: Your data is saved locally in your browser
- **Mobile Optimized**: Designed specifically for iPhone and mobile devices
- **PWA Ready**: Install to your home screen like a native app

## How to Use on iPhone 📱

### Option 1: Local Server (Same Network)

1. **Start the server** on your computer:
   ```bash
   python3 -m http.server 8080
   ```

2. **Find your computer's IP address**:
   - Linux/Mac: Run `hostname -I` or `ifconfig`
   - Windows: Run `ipconfig`

3. **Open on iPhone**:
   - Make sure iPhone is on the same Wi-Fi network
   - Open Safari and go to: `http://YOUR_IP:8080`
   - Example: `http://192.168.1.100:8080`

4. **Install to Home Screen**:
   - Tap the Share button (square with arrow)
   - Scroll down and tap "Add to Home Screen"
   - Tap "Add" - now it's like a real app!

### Option 2: Deploy Online (Recommended)

Deploy to any free hosting service:

- **GitHub Pages** (free)
- **Netlify** (free)
- **Vercel** (free)
- **Cloudflare Pages** (free)

Then access from anywhere via the URL!

## Tech Stack 🛠️

- Pure HTML5, CSS3, JavaScript
- No dependencies or frameworks
- LocalStorage for data persistence
- Progressive Web App (PWA) capabilities
- Mobile-first responsive design

## Features in Detail

### Notes Tab
- Write multi-line notes up to 500 characters
- Press Cmd+Enter to quickly add notes
- Each note shows timestamp
- Easy delete functionality

### Tasks Tab
- Create quick to-do items
- Check off completed tasks
- Visual strikethrough for completed items
- Track progress in footer

### Dark Mode
- Beautiful dark theme for night use
- Smooth transitions
- Preference saved automatically

### Responsive Design
- Optimized for iPhone screen sizes
- Works on all mobile devices
- Clean, modern interface
- Touch-friendly buttons

## Browser Support

- iOS Safari ✅
- Chrome Mobile ✅
- Firefox Mobile ✅
- Any modern mobile browser ✅

## Data Storage

All your notes and tasks are stored locally in your browser using LocalStorage. This means:
- ✅ Your data stays private (never leaves your device)
- ✅ Works offline
- ✅ No account needed
- ⚠️ Clearing browser data will delete your notes

## Enjoy! 🎉

Your QuickNotes app is ready to use. Open `index.html` in any browser or follow the instructions above to use it on your iPhone!
