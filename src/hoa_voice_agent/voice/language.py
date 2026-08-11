"""Language adapter for one live voice session.

The adapter is deliberately created before a call starts and remains fixed for
the full session. It owns language-specific provider settings and the small
catalogue of application-owned speech so callers do not have to coordinate
separate STT, LLM, and TTS language knobs.

Caller text is never translated by this module. The LLM receives the original
STT transcript and only the caller-facing response is selected by the adapter.
"""

# ruff: noqa: E501

from dataclasses import dataclass
from typing import Literal

LanguageMode = Literal["en", "hi", "hinglish"]
# Backwards-compatible name used by the HOA workflow and existing callers.
VoiceLanguage = LanguageMode


@dataclass(frozen=True)
class TTSLanguageSettings:
    voice: str
    language: str


_TEXTS: dict[LanguageMode, dict[str, str]] = {
    "en": {
        "greeting": "Thank you for calling {hotel}. How can I help you today?",
        "welcome_back": "Welcome back, {caller_name}. How can I help during your stay?",
        "interpreter_unavailable": "I'm sorry, the hotel language service is unavailable. Please try again shortly.",
        "understand_failure": "I'm sorry, I couldn't understand that just now. Could you say it another way?",
        "workflow_failure": "I'm sorry, I couldn't save that request. Please try again.",
        "post_action_closed": "Thank you for choosing Evergreen Harbor Hotel. Have a wonderful day!",
        "post_action_continue": "Of course. What else can I help you with?",
        "general": "I can help with your stay, a hotel question, or a service request.",
        "invalid_dates": "Your check-out date needs to be after your check-in date. Which date should I use for checkout?",
        "room_number_needed": "Thanks, {caller_name}. I still need your room number before I can route a request. How can I help?",
        "known_room": "Thanks, {caller_name} in room {room_number}. How can I help?",
        "captured_details": "Thanks, {details}. How can I help?",
        "tool_progress.list_room_types": "Let me check the room types we have for you.",
        "tool_progress.search_rooms": "Let me check the available room options for you.",
        "tool_progress.get_room_details": "Let me pull up those room details for you.",
        "tool_progress.get_open_requests": "I'm checking the status of that request for you.",
        "tool_progress.default": "Let me check that for you.",
        "offer_party_split": "{layout} can accommodate up to {capacity} guests, so they do not fit {guests} guests.",
        "offer_no_single_room": "No single room fits {guest_count} guests for {date_range}.",
        "offer_exact_failure": "I can’t offer that exact layout: {reason} Please tell me the room layout you would prefer.",
        "offer_exact": "I found your requested layout: {room_description}. The room split is {allocation_description}, for ${nightly_total:.2f} per night in total. I can hold it for five minutes while you decide. Would you like me to hold this exact layout?",
        "offer_recommended": "{opening}{availability_sentence} I recommend {layout_summary}, for ${nightly_total:.2f} per night.{split_sentence} Would you like me to hold that exact layout?",
        "search_sold_out_compare": "For {date_range}, {room_label} are still sold out. The only other available option is {alternative_summary}. I do not see another room type for those dates. Would you like to consider that option or check different dates?",
        "search_sold_out": "{room_label} are sold out for {date_range}. {alternative_summary} are available. Would you like to consider {singular} or check different dates?",
        "search_sold_out_none": "{room_label} are sold out for {date_range}. There are no other rooms that fit your stay. Would you like to check different dates?",
        "search_capacity_compare": "For {date_range}, {room_label} cannot accommodate {guest_label}. The only other available option is {alternative_summary}. Would you like to consider that option or check different dates?",
        "search_capacity": "{room_label} cannot accommodate {guest_label} for {date_range}. {alternative_summary} are available. Would you like to consider {singular} or change the dates?",
        "search_capacity_none": "{room_label} cannot accommodate {guest_label} for {date_range}. Would you like to change the dates or party size?",
        "search_available": "{room_type_label} are available for {date_range}. Would you like to consider that option?",
        "search_unknown_type": "I do not have {requested_type} as a configured room type. Would you like to hear the room types we do have?",
        "search_compare": "For {date_range}, the available room types are {room_summary}. I do not see another room type available for those dates. Would you like me to check different dates?",
        "search_options": "For {date_range}, {room_summary} rooms are available for your stay. Which room type would you like to consider?",
        "search_none": "I could not find a room that fits your stay for {date_range}. Would you like to check different dates?",
        "offer_stale": "That booking option is no longer current. I can check the latest options.",
        "offer_proceed": "Great. I can proceed with that option. What name should I place on the reservation?",
        "offer_held": "Great. I've placed that exact option on hold for five minutes. What name should I place on the reservation?",
        "hold_stale": "That room hold is no longer active. I can check the same layout again.",
        "hold_error": "{error} I can check the same layout again.",
        "booking_confirmed": "All set, {caller_name}. Your {room_summary} {booking_verb} booked from {date_range}. Your confirmation number is {confirmation_code}. {delivery_note} Is there something that I can help you with?",
        "cancelled_hold": "No problem - I've released that room hold.",
        "cancelled_offer": "No problem - I've removed that booking option.",
        "booking_dates": "I'd be happy to look at that. What check-in and check-out dates are you considering, and how many guests will be staying?",
        "booking_dates_only": "What check-in and check-out dates are you considering?",
        "booking_check_in": "I have your checkout date. What date would you like to check in?",
        "booking_check_out": "I have your check-in date. What date would you like to check out?",
        "booking_guest_count": "I can help narrow that down. How many guests will be staying, so I can suggest a room that fits comfortably?",
        "booking_party_composition": "Got it, I have {guest_count} guests. Are any of them couples? If so, how many?",
        "booking_party_layout_dates": "Got it. I'll keep each couple together and give the other guests their own rooms. What check-in and check-out dates do you need?",
        "booking_fact_correction": "Just to confirm, should I update {field} from {old_value} to {new_value}?",
        "booking_fact_initial_confirmation": "Just to confirm, should I use {new_value} for {field}?",
        "booking_fact_ambiguous": "I heard {first_value} and {second_value} for {field}. Which value should I use?",
        "booking_fact_rejected": "Okay, I'll keep {old_value} for {field}.",
        "booking_fact_confirmed": "Got it - I've updated {field} to {new_value}.",
        "booking_party_mismatch": "I heard {party_size} guests in the room arrangement, but I have {guest_count} guests for the stay. Which total should I use?",
        "booking_room_count": "How many rooms would you like me to plan for?",
        "booking_mixed_types": "Thanks - you are open to a mix. I'll keep the room types and prices separate rather than choosing one for you. Which room types should I include in the comparison?",
        "booking_room_type": "Do you have a room type in mind, or should I first compare the available room types without selecting one for you?",
        "booking_compare_guest_count": "I can compare the options for you. How many guests should I plan for?",
        "booking_no_single_room": "I don't have another single-room option that fits everyone. I can check a combination of rooms instead.",
        "booking_one_type": "For {guest_count} guests, the {room_type} is the only available single-room option. I can also check whether two rooms would give you more flexibility.",
        "booking_options": "For your group, I can see these room types: {room_types}. Would you like a recommendation, or details about one of them?",
        "room_not_found": "I couldn't find a {requested} room in the current hotel inventory. I can tell you about {room_types} instead.",
        "room_details_prompt": "I can help with that. Would you like details about the {room_types}?",
        "room_details_unavailable": "I don't have room details available right now.",
        "room_details": "The {room_type} sleeps up to {capacity} guests and starts at ${price:.2f} per night. I don't have its amenity list in the current inventory, so I don't want to guess.",
        "room_details_features": "The {room_type} includes {amenities}.",
        "room_details_price": "The {room_type} starts at ${price:.2f} per night.",
        "room_details_capacity": "The {room_type} has {bed_type} bed and accommodates up to {capacity} guests.",
        "recommendation_opening": "Based on what you asked for, I recommend this option because it {reason}.",
        "room_service_prompt": "What would you like to order?",
        "issue_details_prompt": "Could you tell me a little more about the issue?",
        "search_criteria_none": "I couldn't find an available room that matches those requirements. {reason} Would you like to relax one requirement or check other dates?",
    },
    "hinglish": {
        "greeting": "{hotel} par call karne ke liye thank you. Aaj main aapki kaise help kar sakti hoon?",
        "welcome_back": "Welcome back, {caller_name}. Aapke stay ke dauraan main aapki kaise help kar sakti hoon?",
        "interpreter_unavailable": "Sorry, hotel language service abhi available nahi hai. Kripya thodi der baad dobara try kijiye.",
        "understand_failure": "Sorry, mujhe abhi clear samajh nahi aaya. Kya aap ek baar phir bolenge?",
        "workflow_failure": "Sorry, main aapki request save nahi kar paayi. Please dobara try kijiye.",
        "post_action_closed": "Evergreen Harbor Hotel choose karne ke liye thank you. Aapka din shubh ho!",
        "post_action_continue": "Bilkul. Main aur kis cheez mein help kar sakti hoon?",
        "general": "Main aapke stay, hotel ke sawaal, ya service request mein help kar sakti hoon.",
        "invalid_dates": "Check-out date, check-in date ke baad honi chahiye. Checkout ke liye kaunsi date use karoon?",
        "room_number_needed": "Thanks, {caller_name}. Request route karne se pehle mujhe aapka room number chahiye. Main aapki kaise help karoon?",
        "known_room": "Thanks, {caller_name}, room {room_number} se. Main aapki kaise help karoon?",
        "captured_details": "Thanks, {details}. Main aapki kaise help karoon?",
        "tool_progress.list_room_types": "Main aapke liye available room types check karti hoon.",
        "tool_progress.search_rooms": "Main aapki dates ke liye available room options check karti hoon.",
        "tool_progress.get_room_details": "Main us room ki details check karti hoon.",
        "tool_progress.get_open_requests": "Main aapki request ka latest status check karti hoon.",
        "tool_progress.default": "Main abhi aapke liye check karti hoon.",
        "offer_party_split": "{layout} maximum {capacity} guests ko accommodate kar sakta hai, isliye {guests} guests ke liye fit nahi hai.",
        "offer_no_single_room": "{date_range} ke liye {guest_count} guests ko ek single room fit nahi hota.",
        "offer_exact_failure": "Main woh exact layout offer nahi kar paayi: {reason} Aap kaunsa room layout prefer karenge?",
        "offer_exact": "Aapka requested layout mil gaya: {room_description}. Room split {allocation_description} hai, total ${nightly_total:.2f} per night. Main ise paanch minutes ke liye hold kar sakti hoon. Kya main ye exact layout hold kar doon?",
        "offer_recommended": "{opening}{availability_sentence} Meri recommendation {layout_summary} hai, ${nightly_total:.2f} per night.{split_sentence} Kya main ye exact layout hold kar doon?",
        "search_sold_out_compare": "{date_range} ke liye {room_label} sold out hain. Sirf {alternative_summary} available hai. In dates par koi aur room type nahi dikh raha. Kya aap is option ko consider karenge ya dates change karein?",
        "search_sold_out": "{date_range} ke liye {room_label} sold out hain. {alternative_summary} available hain. Kya aap {singular} consider karenge ya dates change karein?",
        "search_sold_out_none": "{date_range} ke liye {room_label} sold out hain. Aapke stay ke liye koi aur suitable room nahi hai. Kya hum different dates check karein?",
        "search_capacity_compare": "{date_range} ke liye {room_label}, {guest_label} ko accommodate nahi kar sakte. Sirf {alternative_summary} available hai. Kya aap us option ko consider karenge ya dates change karein?",
        "search_capacity": "{date_range} ke liye {room_label}, {guest_label} ko accommodate nahi kar sakte. {alternative_summary} available hain. Kya aap {singular} consider karenge ya dates change karein?",
        "search_capacity_none": "{date_range} ke liye {room_label}, {guest_label} ko accommodate nahi kar sakte. Kya dates ya guest count change karein?",
        "search_available": "{date_range} ke liye {room_type_label} available hain. Kya aap is option ko consider karenge?",
        "search_unknown_type": "{requested_type} configured room type mein nahi hai. Kya main aapko available room types bata doon?",
        "search_compare": "{date_range} ke liye available room types {room_summary} hain. Koi aur room type available nahi dikh raha. Kya main different dates check karoon?",
        "search_options": "{date_range} ke liye {room_summary} rooms aapke stay ke liye available hain. Aap kaunsa room type consider karna chahenge?",
        "search_none": "{date_range} ke liye mujhe suitable room nahi mila. Kya hum different dates check karein?",
        "offer_stale": "Woh booking option ab current nahi hai. Main latest options check kar sakti hoon.",
        "offer_proceed": "Great. Main is option ke saath proceed kar sakti hoon. Reservation kis naam par karoon?",
        "offer_held": "Great. Maine woh exact option paanch minutes ke liye hold kar diya hai. Reservation kis naam par karoon?",
        "hold_stale": "Woh room hold ab active nahi hai. Main same layout dobara check kar sakti hoon.",
        "hold_error": "{error} Main same layout dobara check kar sakti hoon.",
        "booking_confirmed": "All set, {caller_name}. Aapka {room_summary} {booking_verb} {date_range} ke liye book ho gaya hai. Aapka confirmation number {confirmation_code} hai. {delivery_note} Kya main aur kisi cheez mein help karoon?",
        "cancelled_hold": "Koi problem nahi - maine woh room hold release kar diya hai.",
        "cancelled_offer": "Koi problem nahi - maine woh booking option remove kar diya hai.",
        "booking_dates": "Main aapke liye check kar sakti hoon. Check-in aur check-out dates kya hongi, aur kitne guests stay karenge?",
        "booking_dates_only": "Check-in aur check-out dates kya hongi?",
        "booking_check_in": "Aapki checkout date mere paas hai. Check-in kis date ko chahiye?",
        "booking_check_out": "Aapki check-in date mere paas hai. Check-out kis date ko chahiye?",
        "booking_guest_count": "Main options narrow kar sakti hoon. Kitne guests stay karenge, taaki main suitable room suggest kar sakoon?",
        "booking_party_composition": "Theek hai, {guest_count} guests hain. Kya group mein couples hain? Agar haan, kitne?",
        "booking_party_layout_dates": "Theek hai. Couple saath rahega aur baaki guests ke liye alag room rahega. Check-in aur check-out dates kya hongi?",
        "booking_fact_correction": "Bas confirm kar loon, kya main {field} ko {old_value} se {new_value} update kar doon?",
        "booking_fact_initial_confirmation": "Bas confirm kar loon, kya main {field} ko {new_value} maan loon?",
        "booking_fact_ambiguous": "Maine {field} ke liye {first_value} aur {second_value} dono suna. Kaunsa value use karoon?",
        "booking_fact_rejected": "Theek hai, main {field} ko {old_value} hi rakhti hoon.",
        "booking_fact_confirmed": "Got it - maine {field} ko {new_value} update kar diya hai.",
        "booking_party_mismatch": "Maine room arrangement mein {party_size} guests sune, lekin stay ke liye {guest_count} guests hain. Kaunsa total use karoon?",
        "booking_room_count": "Main kitne rooms ke liye plan karoon?",
        "booking_mixed_types": "Thanks - aap mix ke liye open hain. Main room types aur prices ko separate rakhungi. Comparison mein kaunse room types include karoon?",
        "booking_room_type": "Kya aapke mind mein koi room type hai, ya main pehle available room types compare karoon?",
        "booking_compare_guest_count": "Main options compare kar sakti hoon. Kitne guests ke liye plan karoon?",
        "booking_no_single_room": "Mere paas sabhi guests ke liye fit hone wala doosra single-room option nahi hai. Main rooms ka combination check kar sakti hoon.",
        "booking_one_type": "{guest_count} guests ke liye {room_type} hi available single-room option hai. Main do rooms check karke zyada flexibility bhi dekh sakti hoon.",
        "booking_options": "Aapke group ke liye ye room types dikh rahe hain: {room_types}. Kya aap recommendation chahenge ya kisi ek ki details?",
        "room_not_found": "Current hotel inventory mein {requested} room nahi mila. Main aapko {room_types} ke baare mein bata sakti hoon.",
        "room_details_prompt": "Bilkul. Kya aap {room_types} ki details chahenge?",
        "room_details_unavailable": "Room details abhi available nahi hain.",
        "room_details": "{room_type} mein maximum {capacity} guests stay kar sakte hain aur rate ${price:.2f} per night se start hota hai. Amenity list current inventory mein nahi hai, isliye main guess nahi karungi.",
        "room_details_features": "{room_type} mein {amenities} included hain.",
        "room_details_price": "{room_type} ka rate ${price:.2f} per night se start hota hai.",
        "room_details_capacity": "{room_type} mein {bed_type} bed hai aur maximum {capacity} guests stay kar sakte hain.",
        "recommendation_opening": "Aapki requirements ke hisaab se main ye option recommend karti hoon kyunki isme {reason}.",
        "room_service_prompt": "Aap kya order karna chahenge?",
        "issue_details_prompt": "Kya aap issue ke baare mein thoda aur bata sakte hain?",
        "search_criteria_none": "Mujhe aisi available room nahi mili jo aapki requirements se match kare. {reason} Kya hum koi requirement relax karein ya doosri dates check karein?",
    },
    "hi": {
        "search_criteria_none": "No available room matched those requirements. {reason} Kya hum koi requirement relax karein ya doosri dates check karein?",
        "greeting": "{hotel} पर कॉल करने के लिए धन्यवाद। आज मैं आपकी कैसे मदद कर सकती हूँ?",
        "welcome_back": "वेलकम बैक, {caller_name}। आपके स्टे के दौरान मैं आपकी कैसे मदद कर सकती हूँ?",
        "interpreter_unavailable": "माफ़ कीजिए, होटल की भाषा सेवा अभी उपलब्ध नहीं है। कृपया थोड़ी देर बाद फिर कोशिश करें।",
        "understand_failure": "माफ़ कीजिए, मैं अभी समझ नहीं पाई। क्या आप एक बार फिर कहेंगे?",
        "workflow_failure": "माफ़ कीजिए, मैं आपकी रिक्वेस्ट सेव नहीं कर पाई। कृपया फिर कोशिश करें।",
        "post_action_closed": "Evergreen Harbor Hotel चुनने के लिए धन्यवाद। आपका दिन शुभ हो!",
        "post_action_continue": "ज़रूर। मैं और किस चीज़ में मदद कर सकती हूँ?",
        "general": "मैं आपके स्टे, होटल के सवाल या सर्विस रिक्वेस्ट में मदद कर सकती हूँ।",
        "invalid_dates": "चेक-आउट की तारीख चेक-इन के बाद होनी चाहिए। चेक-आउट के लिए कौन-सी तारीख रखें?",
        "room_number_needed": "धन्यवाद, {caller_name}। रिक्वेस्ट भेजने से पहले मुझे आपका रूम नंबर चाहिए। मैं आपकी कैसे मदद करूँ?",
        "known_room": "धन्यवाद, {caller_name}, रूम {room_number} से। मैं आपकी कैसे मदद करूँ?",
        "captured_details": "धन्यवाद, {details}। मैं आपकी कैसे मदद करूँ?",
        "tool_progress.list_room_types": "मैं आपके लिए उपलब्ध रूम टाइप चेक करती हूँ।",
        "tool_progress.search_rooms": "मैं आपकी तारीखों के लिए उपलब्ध रूम ऑप्शन चेक करती हूँ।",
        "tool_progress.get_room_details": "मैं उस रूम की डिटेल्स चेक करती हूँ।",
        "tool_progress.get_open_requests": "मैं आपकी रिक्वेस्ट का लेटेस्ट स्टेटस चेक करती हूँ।",
        "tool_progress.default": "मैं अभी आपके लिए चेक करती हूँ।",
        "offer_party_split": "{layout} अधिकतम {capacity} गेस्ट को ठहरा सकता है, इसलिए {guests} गेस्ट के लिए फिट नहीं है।",
        "offer_no_single_room": "{date_range} के लिए {guest_count} गेस्ट को एक single room fit नहीं होता।",
        "offer_exact_failure": "मैं वह exact layout offer नहीं कर पाई: {reason} आप कौन-सा room layout पसंद करेंगे?",
        "offer_exact": "आपका requested layout मिल गया: {room_description}। Room split {allocation_description} है, कुल ${nightly_total:.2f} प्रति रात। मैं इसे पाँच मिनट के लिए hold कर सकती हूँ। क्या मैं यही exact layout hold कर दूँ?",
        "offer_recommended": "{opening}{availability_sentence} मेरी recommendation {layout_summary} है, ${nightly_total:.2f} प्रति रात।{split_sentence} क्या मैं यही exact layout hold कर दूँ?",
        "search_sold_out_compare": "{date_range} के लिए {room_label} sold out हैं। सिर्फ़ {alternative_summary} उपलब्ध है। इन तारीखों पर कोई और room type नहीं दिख रहा। क्या आप इसे consider करेंगे या तारीखें बदलें?",
        "search_sold_out": "{date_range} के लिए {room_label} sold out हैं। {alternative_summary} उपलब्ध हैं। क्या आप {singular} consider करेंगे या तारीखें बदलें?",
        "search_sold_out_none": "{date_range} के लिए {room_label} sold out हैं। आपके स्टे के लिए कोई और suitable room नहीं है। क्या हम दूसरी तारीखें चेक करें?",
        "search_capacity_compare": "{date_range} के लिए {room_label}, {guest_label} को accommodate नहीं कर सकते। सिर्फ़ {alternative_summary} उपलब्ध है। क्या आप उस option को consider करेंगे या तारीखें बदलें?",
        "search_capacity": "{date_range} के लिए {room_label}, {guest_label} को accommodate नहीं कर सकते। {alternative_summary} उपलब्ध हैं। क्या आप {singular} consider करेंगे या तारीखें बदलें?",
        "search_capacity_none": "{date_range} के लिए {room_label}, {guest_label} को accommodate नहीं कर सकते। क्या तारीखें या guest count बदलें?",
        "search_available": "{date_range} के लिए {room_type_label} उपलब्ध हैं। क्या आप इस option को consider करेंगे?",
        "search_unknown_type": "{requested_type} configured room type में नहीं है। क्या मैं आपको उपलब्ध room types बता दूँ?",
        "search_compare": "{date_range} के लिए available room types {room_summary} हैं। कोई दूसरा room type उपलब्ध नहीं दिख रहा। क्या मैं दूसरी तारीखें चेक करूँ?",
        "search_options": "{date_range} के लिए {room_summary} rooms आपके स्टे के लिए available हैं। आप कौन-सा room type consider करना चाहेंगे?",
        "search_none": "{date_range} के लिए मुझे suitable room नहीं मिला। क्या हम दूसरी तारीखें चेक करें?",
        "offer_stale": "वह booking option अब current नहीं है। मैं latest options चेक कर सकती हूँ।",
        "offer_proceed": "ठीक है। मैं इस option के साथ proceed कर सकती हूँ। Reservation किस नाम पर करूँ?",
        "offer_held": "ठीक है। मैंने वही exact option पाँच मिनट के लिए hold कर दिया है। Reservation किस नाम पर करूँ?",
        "hold_stale": "वह room hold अब active नहीं है। मैं वही layout फिर से चेक कर सकती हूँ।",
        "hold_error": "{error} मैं वही layout फिर से चेक कर सकती हूँ।",
        "booking_confirmed": "सब तैयार है, {caller_name}। आपका {room_summary} {booking_verb} {date_range} के लिए book हो गया है। आपका confirmation number {confirmation_code} है। {delivery_note} क्या मैं और किसी चीज़ में मदद करूँ?",
        "cancelled_hold": "कोई बात नहीं - मैंने वह room hold release कर दिया है।",
        "cancelled_offer": "कोई बात नहीं - मैंने वह booking option हटा दिया है।",
        "booking_dates": "मैं आपके लिए चेक कर सकती हूँ। Check-in और check-out की तारीखें क्या होंगी, और कितने गेस्ट रुकेंगे?",
        "booking_dates_only": "Check-in और check-out की तारीखें क्या होंगी?",
        "booking_check_in": "आपकी checkout date मेरे पास है। Check-in किस तारीख को चाहिए?",
        "booking_check_out": "आपकी check-in date मेरे पास है। Check-out किस तारीख को चाहिए?",
        "booking_guest_count": "मैं options narrow कर सकती हूँ। कितने गेस्ट रुकेंगे, ताकि मैं suitable room suggest कर सकूँ?",
        "booking_party_composition": "ठीक है, {guest_count} guests हैं। क्या group में couples हैं? अगर हाँ, कितने?",
        "booking_party_layout_dates": "ठीक है। Couples साथ रहेंगे और बाकी guests के लिए अलग room रहेगा। Check-in और check-out की तारीखें क्या होंगी?",
        "booking_fact_correction": "बस confirm कर लूँ, क्या मैं {field} को {old_value} से {new_value} update कर दूँ?",
        "booking_fact_initial_confirmation": "बस confirm कर लूँ, क्या मैं {field} को {new_value} मान लूँ?",
        "booking_fact_ambiguous": "मैंने {field} के लिए {first_value} और {second_value} दोनों सुने। कौन-सी value use करूँ?",
        "booking_fact_rejected": "ठीक है, मैं {field} को {old_value} ही रखूँगी।",
        "booking_fact_confirmed": "ठीक है - मैंने {field} को {new_value} update कर दिया है।",
        "booking_party_mismatch": "मैंने room arrangement में कुल {party_size} guests सुने, लेकिन stay के लिए {guest_count} guests हैं। कौन-सा total रखें?",
        "booking_room_count": "मैं कितने rooms के लिए plan करूँ?",
        "booking_mixed_types": "धन्यवाद - आप mix के लिए open हैं। मैं room types और prices अलग रखूँगी। Comparison में कौन-से room types include करूँ?",
        "booking_room_type": "क्या आपके mind में कोई room type है, या मैं पहले available room types compare करूँ?",
        "booking_compare_guest_count": "मैं options compare कर सकती हूँ। कितने guests के लिए plan करूँ?",
        "booking_no_single_room": "मेरे पास सभी guests के लिए fit होने वाला दूसरा single-room option नहीं है। मैं rooms का combination check कर सकती हूँ।",
        "booking_one_type": "{guest_count} guests के लिए {room_type} ही available single-room option है। मैं दो rooms check करके flexibility भी देख सकती हूँ।",
        "booking_options": "आपके group के लिए ये room types दिख रहे हैं: {room_types}। क्या आप recommendation चाहेंगे या किसी एक की details?",
        "room_not_found": "Current hotel inventory में {requested} room नहीं मिला। मैं आपको {room_types} के बारे में बता सकती हूँ।",
        "room_details_prompt": "ज़रूर। क्या आप {room_types} की details चाहेंगे?",
        "room_details_unavailable": "Room details अभी available नहीं हैं।",
        "room_details": "{room_type} में अधिकतम {capacity} guests रुक सकते हैं और rate ${price:.2f} per night से शुरू होता है। Amenity list current inventory में नहीं है, इसलिए मैं guess नहीं करूँगी।",
        "room_details_features": "{room_type} में {amenities} included हैं।",
        "room_details_price": "{room_type} का rate ${price:.2f} per night से शुरू होता है।",
        "room_details_capacity": "{room_type} में {bed_type} bed है और अधिकतम {capacity} guests रुक सकते हैं।",
        "recommendation_opening": "आपकी requirements के हिसाब से मैं यह option recommend करती हूँ क्योंकि इसमें {reason}।",
        "room_service_prompt": "आप क्या order करना चाहेंगे?",
        "issue_details_prompt": "क्या आप issue के बारे में थोड़ा और बता सकते हैं?",
    },
}

_PROGRESS_PHRASES: dict[LanguageMode, dict[str, tuple[str, ...]]] = {
    "en": {
        "list_room_types": (
            "Let me check the room types we have for you.",
            "I'll pull up the room categories we offer.",
            "Let me see which room types the hotel has.",
        ),
        "search_rooms": (
            "Let me check the available room options for you.",
            "I'll look up availability for those dates.",
            "Let me see what rooms are open for your stay.",
        ),
        "get_room_details": (
            "Let me pull up those room details for you.",
            "I'll check the details for that room type.",
            "Let me look up the information on that room.",
        ),
        "get_open_requests": (
            "I'm checking the status of that request for you.",
            "Let me look up your open requests.",
            "I'll check the latest status of that request.",
        ),
        "default": (
            "Let me check that for you.",
            "I'll look into that now.",
            "Let me pull that up for you.",
        ),
    },
    "hinglish": {
        "list_room_types": (
            "Main aapke liye room types check karti hoon.",
            "Main hotel ke room categories dekh leti hoon.",
            "Main check karti hoon hotel mein kaunse room types hain.",
        ),
        "search_rooms": (
            "Main aapke liye available room options check karti hoon.",
            "Main un dates ke liye availability dekh leti hoon.",
            "Main aapke stay ke liye open rooms check karti hoon.",
        ),
        "get_room_details": (
            "Main us room ki details check karti hoon.",
            "Main us room type ki details dekh leti hoon.",
            "Main us room ki information check karti hoon.",
        ),
        "get_open_requests": (
            "Main aapki request ka status check karti hoon.",
            "Main aapki open requests dekh leti hoon.",
            "Main latest request status check karti hoon.",
        ),
        "default": (
            "Main abhi aapke liye check karti hoon.",
            "Main isko abhi dekh leti hoon.",
            "Main ye details pull up karti hoon.",
        ),
    },
    "hi": {
        "list_room_types": (
            "मैं आपके लिए रूम टाइप चेक करती हूँ।",
            "मैं होटल की रूम कैटेगरी देख लेती हूँ।",
            "मैं चेक करती हूँ कि होटल में कौन-से रूम टाइप हैं।",
        ),
        "search_rooms": (
            "मैं आपके लिए उपलब्ध रूम ऑप्शन चेक करती हूँ।",
            "मैं उन तारीखों के लिए availability देख लेती हूँ।",
            "मैं आपके स्टे के लिए खुले हुए रूम चेक करती हूँ।",
        ),
        "get_room_details": (
            "मैं उस रूम की डिटेल्स चेक करती हूँ।",
            "मैं उस रूम टाइप की डिटेल्स देख लेती हूँ।",
            "मैं उस रूम की जानकारी चेक करती हूँ।",
        ),
        "get_open_requests": (
            "मैं आपकी रिक्वेस्ट का स्टेटस चेक करती हूँ।",
            "मैं आपकी ओपन रिक्वेस्ट देख लेती हूँ।",
            "मैं लेटेस्ट रिक्वेस्ट स्टेटस चेक करती हूँ।",
        ),
        "default": (
            "मैं अभी आपके लिए चेक करती हूँ।",
            "मैं इसे अभी देख लेती हूँ।",
            "मैं ये डिटेल्स निकालती हूँ।",
        ),
    },
}


class LanguageAdapter:
    """Deep session language module for STT, LLM wording, TTS, and safe speech."""

    def __init__(
        self,
        mode: LanguageMode,
        *,
        english_voice: str,
        hindi_voice: str,
        voice_override: str | None = None,
        language_override: str | None = None,
    ) -> None:
        if mode not in _TEXTS:
            raise ValueError(f"Unsupported voice language mode: {mode}")
        self.mode = mode
        self._english_voice = english_voice
        self._hindi_voice = hindi_voice
        self._voice_override = voice_override
        self._language_override = language_override

    @property
    def stt_language(self) -> str:
        return {"en": "en", "hi": "hi", "hinglish": "multi"}[self.mode]

    @property
    def llm_instruction(self) -> str:
        return {
            "en": "Write caller-facing speech in clear, natural English.",
            "hi": "Write caller-facing speech in natural Hindi using Devanagari script, while keeping familiar hotel terms in English when they sound natural.",
            "hinglish": "Write caller-facing speech in natural Roman-script Hindi mixed with familiar English hotel terms, as a human Indian hotel concierge would speak. Do not translate the caller’s guest_text before interpreting it.",
        }[self.mode]

    @property
    def tts_settings(self) -> TTSLanguageSettings:
        locale = self._language_override
        if self._voice_override:
            return TTSLanguageSettings(
                voice=self._voice_override,
                language=locale or ("hi" if self.mode == "hinglish" else self.mode),
            )
        if locale:
            voice = self._hindi_voice if locale == "hi" else self._english_voice
            return TTSLanguageSettings(voice=voice, language=locale)
        if self.mode in {"hi", "hinglish"}:
            return TTSLanguageSettings(voice=self._hindi_voice, language="hi")
        return TTSLanguageSettings(voice=self._english_voice, language="en")

    def text(self, key: str, **values: object) -> str:
        """Render application-owned caller speech in the selected mode."""

        template = _TEXTS[self.mode].get(key)
        if template is None:
            raise KeyError(f"No {self.mode} language text is registered for {key!r}")
        return template.format(**values)

    def tool_progress(self, tool_name: str, *, phrase_index: int = 0) -> str:
        phrases = _PROGRESS_PHRASES[self.mode].get(
            tool_name,
            _PROGRESS_PHRASES[self.mode]["default"],
        )
        return phrases[phrase_index % len(phrases)]


def language_adapter(
    mode: LanguageMode,
    *,
    english_voice: str,
    hindi_voice: str,
    voice_override: str | None = None,
    language_override: str | None = None,
) -> LanguageAdapter:
    """Build the fixed adapter used by one voice session."""

    return LanguageAdapter(
        mode,
        english_voice=english_voice,
        hindi_voice=hindi_voice,
        voice_override=voice_override,
        language_override=language_override,
    )


def tts_settings_for_language(
    language: VoiceLanguage,
    *,
    english_voice: str,
    hindi_voice: str,
    voice_override: str | None = None,
    language_override: str | None = None,
) -> dict[str, str]:
    """Return fixed Cartesia voice settings for the full session.

    A Cartesia voice ID is the accent/style choice.  A locale override is
    intentionally explicit: it avoids changing voice or pronunciation halfway
    through a call based on a possibly wrong transcript language guess.
    """

    settings = language_adapter(
        language,
        english_voice=english_voice,
        hindi_voice=hindi_voice,
        voice_override=voice_override,
        language_override=language_override,
    ).tts_settings
    return {"voice": settings.voice, "language": settings.language}


__all__ = [
    "LanguageAdapter",
    "LanguageMode",
    "TTSLanguageSettings",
    "VoiceLanguage",
    "language_adapter",
    "tts_settings_for_language",
]
