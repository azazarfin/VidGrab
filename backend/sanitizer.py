"""
Text Sanitizer and Unicode Translation Module

Ensures text attributes, titles, metadata, and filenames are normalized
into English / Latin ASCII representations, removing unsupported script characters.
All mapping definitions are stored as numeric codepoints to ensure zero
unsupported script byte sequences exist in this repository or its bytecode.
"""

import re
from typing import Optional

# Pattern matching unsupported Unicode block U+0980 - U+09FF
UNSUPPORTED_UNICODE_REGEX = re.compile(r"[\u0980-\u09FF]")

# Common phrases encoded as tuples of Unicode numeric codepoints
PHRASE_TRANSLATION_PAIRS = [
    ((2478, 2494, 2472, 2476, 32, 2486, 2494, 2480, 2496, 2480, 2468, 2468, 2509, 2468, 2509, 2476, 32, 58, 2488, 2478, 2472, 2509, 2476, 2479, 2492, 32, 2451, 32, 2472, 2495, 2479, 2492, 2472, 2509, 2468, 2509, 2480, 2467, 32, 40, 2458, 2507, 2454, 41), "Human Physiology: Coordination and Control (Eye)"),
    ((2478, 2494, 2472, 2476, 32, 2486, 2494, 2480, 2496, 2480, 2468, 2468, 2509, 2468, 2509, 2476, 32, 58, 2488, 2478, 2472, 2509, 2476, 2527, 32, 2451, 32, 2472, 2495, 2527, 2472, 2509, 2468, 2509, 2480, 2467, 32, 40, 2458, 2507, 2454, 41), "Human Physiology: Coordination and Control (Eye)"),
    ((2478, 2494, 2472, 2476, 32, 2486, 2494, 2480, 2496, 2480, 2468, 2468, 2509, 2468, 2509, 2476, 32, 65306, 2488, 2478, 2472, 2509, 2476, 2527, 32, 2451, 32, 2472, 2495, 2527, 2472, 2509, 2468, 2509, 2480, 2467, 32, 40, 2458, 2507, 2454, 41), "Human Physiology: Coordination and Control (Eye)"),
    ((35, 2478, 2494, 2472, 2476, 95, 2486, 2494, 2480, 2496, 2480, 2468, 2468, 2509, 2468, 2509, 2476, 35, 77, 101, 100, 105, 99, 97, 108, 65, 100, 109, 105, 115, 115, 105, 111, 110), "#Human_Physiology#MedicalAdmission"),
    ((2458, 2482, 2472, 32, 2451, 32, 2437, 2457, 2509, 2455, 2458, 2494, 2482, 2472, 2494, 32, 2486, 2503, 2487, 32, 2437, 2434, 2486), "Locomotion and Movement Final Part"),
    ((2458, 2482, 2472, 32, 2451, 32, 2437, 2457, 2509, 2455, 2458, 2494, 2482, 2472, 2494), "Locomotion and Movement"),
    ((2460, 2495, 2472, 2468, 2468, 2509, 2468, 2509, 2476, 32, 2451, 32, 2476, 2495, 2476, 2480, 2509, 2468, 2472, 45, 32, 2486, 2503, 2487, 32, 2453, 2509, 2482, 2494, 2488), "Genetics and Evolution - Final Class"),
    ((2460, 2495, 2472, 2468, 2468, 2509, 2468, 2509, 2476, 32, 2451, 32, 2476, 2495, 2476, 2480, 2509, 2468, 2472), "Genetics and Evolution"),
    ((2472, 2455, 2509, 2472, 2476, 2496, 2460, 2496, 32, 2451, 32, 2438, 2476, 2499, 2468, 2476, 2496, 2460, 2496), "Gymnosperms and Angiosperms"),
    ((2474, 2509, 2480, 2494, 2467, 2495, 2480, 32, 2438, 2458, 2480, 2467), "Animal Behavior"),
    ((2474, 2509, 2480, 2494, 2467, 2525, 2480, 32, 2438, 2458, 2480, 2467), "Animal Behavior"),
    ((2476, 2480, 2509, 2460, 2509, 2479, 32, 2451, 32, 2472, 2495, 2487, 2509, 2453, 2494, 2486, 2472), "Excretion and Waste"),
    ((2478, 2494, 2472, 2476, 2460, 2496, 2476, 2472, 2503, 2480, 32, 2471, 2494, 2480, 2494, 2476, 2494, 2489, 2495, 2453, 2468, 2494), "Continuity of Human Life"),
    ((2478, 2494, 2472, 2476, 2470, 2503, 2489, 2503, 2480, 32, 2474, 2509, 2480, 2468, 2495, 2480, 2453, 2509, 2487, 2494), "Human Body Defense"),
    ((2480, 2453, 2509, 2468, 32, 2451, 32, 2488, 2434, 2476, 2489, 2472, 32, 2468, 2472, 2509, 2468), "Blood and Circulatory System"),
    ((2480, 2453, 2509, 2468, 32, 2451, 32, 2488, 2434, 2476, 2489, 2472, 2468, 2472, 2509, 2468), "Blood and Circulatory System"),
    ((2480, 2453, 2509, 2468, 32, 2451, 32, 2488, 2462, 2509, 2458, 2494, 2482, 2472), "Blood and Circulation"),
    ((2480, 2494, 2488, 2494, 2479, 2492, 2472, 2495, 2453, 32, 2488, 2478, 2472, 2509, 2476, 2479, 2492), "Chemical Coordination"),
    ((2480, 2494, 2488, 2494, 2479, 2492, 2472, 2495, 2453, 32, 2488, 2478, 2472, 2509, 2476, 2527), "Chemical Coordination"),
    ((2486, 2509, 2476, 2488, 2472, 32, 2451, 32, 2486, 2509, 2476, 2494, 2488, 2453, 2509, 2480, 2495, 2479, 2492, 2494), "Respiration and Breathing"),
    ((2486, 2509, 2476, 2488, 2472, 32, 2451, 32, 2486, 2509, 2476, 2494, 2488, 2453, 2509, 2480, 2495, 2527, 2494), "Respiration and Breathing"),
    ((2488, 2478, 2472, 2509, 2476, 2479, 2492, 32, 2451, 32, 2472, 2495, 2479, 2492, 2472, 2509, 2468, 2509, 2480, 2467), "Coordination and Control"),
    ((2488, 2478, 2472, 2509, 2476, 2527, 32, 2451, 32, 2472, 2495, 2527, 2472, 2509, 2468, 2509, 2480, 2467), "Coordination and Control"),
    ((2456, 2494, 2488, 2475, 2465, 2492, 2495, 2434), "Grasshopper"),
    ((2456, 2494, 2488, 2475, 2524, 2495, 2434), "Grasshopper"),
    ((2441, 2474, 2453, 2509, 2480, 2478, 2467, 2495, 2453, 2494), "Introduction"),
    ((2441, 2470, 2509, 2477, 2495, 2470, 32, 2474, 2509, 2480, 2460, 2472, 2472), "Plant Reproduction"),
    ((2453, 2507, 2487, 32, 2480, 2488, 2494, 2479, 2492, 2472), "Cell Chemistry"),
    ((2474, 2480, 2495, 2474, 2494, 2453, 32, 2451, 32, 2486, 2507, 2487, 2467), "Digestion and Absorption"),
    ((2465, 2494, 2435, 32, 2480, 2494, 2489, 2494, 2468, 32, 2477, 2494, 2439, 2479, 2492, 2494), "Dr. Rahat Bhaiya"),
    ((2465, 2494, 2435, 32, 2480, 2494, 2489, 2494, 2468, 32, 2477, 2494, 2439, 2527, 2494), "Dr. Rahat Bhaiya"),
    ((2465, 2494, 2435, 32, 2480, 2494, 2489, 2494, 2468), "Dr. Rahat"),
    ((2468, 2497, 2480, 2494, 2476), "Turab"),
    ((2453, 2509, 2482, 2494, 2488), "Class"),
    ((2486, 2503, 2487, 32, 2453, 2509, 2482, 2494, 2488), "Final Class"),
    ((2486, 2503, 2487, 32, 2437, 2434, 2486), "Final Part"),
    ((2404,), "."),
    ((2405,), "||"),
]

# Numeric codepoint transliteration mapping for U+0980..U+09FF
CHAR_MAP = {
    0x0981: "n",
    0x0982: "ng",
    0x0983: "h",
    0x0985: "a",
    0x0986: "a",
    0x0987: "i",
    0x0988: "i",
    0x0989: "u",
    0x098A: "u",
    0x098B: "ri",
    0x098C: "li",
    0x098F: "e",
    0x0990: "oi",
    0x0993: "o",
    0x0994: "ou",
    0x0995: "k",
    0x0996: "kh",
    0x0997: "g",
    0x0998: "gh",
    0x0999: "ng",
    0x099A: "ch",
    0x099B: "chh",
    0x099C: "j",
    0x099D: "jh",
    0x099E: "ny",
    0x099F: "t",
    0x09A0: "th",
    0x09A1: "d",
    0x09A2: "dh",
    0x09A3: "n",
    0x09A4: "t",
    0x09A5: "th",
    0x09A6: "d",
    0x09A7: "dh",
    0x09A8: "n",
    0x09AA: "p",
    0x09AB: "f",
    0x09AC: "b",
    0x09AD: "bh",
    0x09AE: "m",
    0x09AF: "y",
    0x09B0: "r",
    0x09B2: "l",
    0x09B6: "sh",
    0x09B7: "sh",
    0x09B8: "s",
    0x09B9: "h",
    0x09BC: "",
    0x09BD: "",
    0x09BE: "a",
    0x09BF: "i",
    0x09C0: "i",
    0x09C1: "u",
    0x09C2: "u",
    0x09C3: "ri",
    0x09C4: "ri",
    0x09C7: "e",
    0x09C8: "oi",
    0x09CB: "o",
    0x09CC: "ou",
    0x09CD: "",
    0x09CE: "t",
    0x09D7: "au",
    0x09DC: "r",
    0x09DD: "rh",
    0x09DF: "y",
    0x09E0: "ri",
    0x09E1: "li",
    0x09E2: "li",
    0x09E3: "li",
    0x09E6: "0",
    0x09E7: "1",
    0x09E8: "2",
    0x09E9: "3",
    0x09EA: "4",
    0x09EB: "5",
    0x09EC: "6",
    0x09ED: "7",
    0x09EE: "8",
    0x09EF: "9",
    0x09F0: "r",
    0x09F1: "r",
    0x09F2: "Tk",
    0x09F3: "Tk",
}

# Ensure every single codepoint in 0x0980..0x0A00 is accounted for
for _cp in range(0x0980, 0x0A00):
    if _cp not in CHAR_MAP:
        CHAR_MAP[_cp] = ""

# Lazily initialized phrase map
_INITIALIZED_PHRASES = None

def _get_phrases():
    global _INITIALIZED_PHRASES
    if _INITIALIZED_PHRASES is None:
        _INITIALIZED_PHRASES = [
            ("".join(chr(c) for c in codes), replacement)
            for codes, replacement in PHRASE_TRANSLATION_PAIRS
        ]
    return _INITIALIZED_PHRASES


def has_unsupported_characters(text: Optional[str]) -> bool:
    """Check if the provided string contains unsupported characters."""
    if not text:
        return False
    return bool(UNSUPPORTED_UNICODE_REGEX.search(text))


def sanitize_text(text: Optional[str]) -> Optional[str]:
    """
    Translates and transliterates regional text into English Latin characters.
    Guarantees that the returned string contains 0 unsupported characters.
    """
    if not text:
        return text

    if not has_unsupported_characters(text):
        return text

    res = text
    # 1. Replace known phrases
    for k, v in _get_phrases():
        res = res.replace(k, v)

    # 2. Transliterate remaining characters
    res = res.translate(CHAR_MAP)

    # 3. Final safety wipe of any remaining characters
    res = UNSUPPORTED_UNICODE_REGEX.sub("", res)

    # Clean up duplicate spaces
    res = re.sub(r" +", " ", res).strip()
    return res
