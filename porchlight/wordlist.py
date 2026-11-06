"""Starter list of hashtag channel names to try against channel hashes nobody has a key for.

Hashtag channels are public by design: the key is sha256("#name")[:16]. Anything named
in decrypted chat is added on top of this list at runtime, and more can be added in settings. Local place
names come from the name sources (the community channel catalog, or your area's CoreScope), not from this list.
"""

_GENERAL = """
test test1 test2 testing tests bot bots mesh meshcore meshnet general chat local talk lounge
hello hi cq ping pings echo random offtopic misc help newbies new welcome
emergency emergencies ems fire police sheriff medical sos 911 sar search rescue
weather wx skywarn storm storms tornado severe spotter spotters nws alerts alert outage power
ares races ham hamradio radio amateur arrl repeater repeaters net nets checkin checkins
lora loranet wardriving wardrive meshmapper mapper map maps coverage range rangetest dx signal
propagation solar space iss satellite sat aurora gps tracker tracking aprs
admin ops operators sysop infra network backbone links routing
news sports events event meetup meetups openhouse party social community neighbors neighborhood
tech iot diy makers maker hackers hackerspace security infosec linux pi raspberrypi esp32
heltec rak meshtastic reticulum mqtt bbs
hiking camping outdoors cycling bike running traffic roads commute
prepper preppers prep survival grid offgrid
family friends kids school church work jobs market forsale trade swap lostfound
music gaming games movies food beer coffee pets dogs cats birds fishing hunting farm garden
usa us midwest
"""


GUESSES = frozenset("#" + w for w in _GENERAL.split())
