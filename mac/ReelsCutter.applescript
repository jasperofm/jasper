-- Reels Cutter app. REPO_DIR is filled in by make_mac_app.sh.
property repoDir : "__REPO_DIR__"

on run
	set f to choose file with prompt "Pick the long video to cut into Reels" of type {"public.movie"}
	cutVideo(f)
end run

on open theFiles
	repeat with f in theFiles
		cutVideo(f)
	end repeat
end open

on cutVideo(f)
	set videoPath to POSIX path of f
	set countAns to text returned of (display dialog "How many Reels should I make?" default answer "5" with title "Reels Cutter")
	try
		set countNum to countAns as integer
	on error
		set countNum to 5
	end try
	set layoutAns to button returned of (display dialog "How should the video fill the vertical frame?" & return & return & "Fill = crop to fill the screen (best when you're in the middle)." & return & "Full frame = whole video on a blurred background." buttons {"Cancel", "Fill", "Full frame"} default button "Full frame" cancel button "Cancel" with title "Reels Cutter")
	if layoutAns is "Fill" then
		set layoutArg to "crop"
	else
		set layoutArg to "blur"
	end if
	set lookPick to choose from list {"none", "warm", "cool", "punchy", "bw"} with prompt "Pick a color look:" default items {"none"} with title "Reels Cutter"
	if lookPick is false then return
	set lookArg to item 1 of lookPick
	set stylePick to choose from list {"classic", "bold", "boxed", "minimal"} with prompt "Pick a caption style:" default items {"classic"} with title "Reels Cutter"
	if stylePick is false then return
	set styleArg to item 1 of stylePick
	set cmd to "clear; cd " & quoted form of repoDir & " && python3 reels_cutter.py " & quoted form of videoPath & " --count " & countNum & " --layout " & layoutArg & " --look " & lookArg & " --style " & styleArg & " --open && echo && echo 'Done! Your Reels are in the gallery that just opened (Movies > Reels Cutter).'"
	tell application "Terminal"
		activate
		do script cmd
	end tell
end cutVideo
