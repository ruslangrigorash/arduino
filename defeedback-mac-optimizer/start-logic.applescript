-- De-Feedback auto-boot script (from the official settings guide).
-- Launches Logic Pro and brings it to the front after login.

tell application "Logic Pro"
    activate
end tell

delay 10

tell application "System Events"
    tell process "Logic Pro"
        set frontmost to true
    end tell
end tell
