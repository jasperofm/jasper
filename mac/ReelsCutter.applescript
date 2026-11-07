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
	set cmd to "clear; cd " & quoted form of repoDir & " && v=" & quoted form of videoPath & " && out=\"${v%.*}_reels\" && python3 reels_cutter.py \"$v\" --count " & countNum & " --layout " & layoutArg & " --out \"$out\" && open \"$out\" && echo && echo 'Done! Your Reels are in the folder that just opened.'"
	tell application "Terminal"
		activate
		do script cmd
	end tell
end cutVideo
