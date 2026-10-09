# Meal Reveal PWA

Αυτό το μικρό PWA δίνει στην εφαρμογή Meal Reveal δικό της όνομα και εικονίδιο
στην αρχική οθόνη κινητού. Ενσωματώνει τη δημόσια εφαρμογή Streamlit με το
επίσημο `?embed=true`. Δεν περιέχει API keys, Supabase keys ή δεδομένα χρηστών.

## Δημοσίευση στο GitHub Pages

1. Δημιουργήστε νέο public repository, π.χ. `meal-reveal-mobile`.
2. Αποσυμπιέστε το ZIP και ανεβάστε **τα περιεχόμενα**, όχι το ίδιο το ZIP.
3. Τα `index.html`, `manifest.webmanifest`, `service-worker.js` και ο φάκελος
   `icons` πρέπει να βρίσκονται στη ρίζα του repository.
4. Ανοίξτε `Settings → Pages`.
5. Στο `Build and deployment`, επιλέξτε `Deploy from a branch`.
6. Επιλέξτε branch `main`, φάκελο `/(root)` και πατήστε `Save`.
7. Περιμένετε να εμφανιστεί η διεύθυνση GitHub Pages και ανοίξτε την από Chrome
   στο Android.
8. Από το μενού του Chrome επιλέξτε `Εγκατάσταση εφαρμογής` ή
   `Προσθήκη στην αρχική οθόνη`.

Αν υπάρχει ήδη παλιό εικονίδιο Streamlit, αφαιρέστε το από την αρχική οθόνη πριν
εγκαταστήσετε τη νέα διεύθυνση GitHub Pages. Αν το Android κρατήσει παλιό
εικονίδιο, κλείστε το Chrome, καθαρίστε την cache του συγκεκριμένου site και
εγκαταστήστε ξανά.

Η ενσωμάτωση iframe υποστηρίζεται μόνο όταν η εφαρμογή Streamlit είναι public.
Το κουμπί `Άνοιγμα σε browser` παραμένει διαθέσιμο ως εναλλακτική.
