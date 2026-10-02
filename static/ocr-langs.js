// ---------------------------------------------------------------------------
// OCR language pair: a main language select plus an optional second one
//
// Page Tools, Export and Convert show two selects of the installed languages:
// #ocrLanguage (English first) and #ocrLanguage2 (optional, for mixed
// documents). This file joins them into the '+'-joined Tesseract spec the
// server expects. The server checks the codes and how many a request may name.
// ---------------------------------------------------------------------------

// 'eng' + 'spa' -> 'eng+spa'. A blank or repeated second language adds
// nothing; a blank first language falls back to 'eng'.
function joinLanguages(first, second) {
    const a = String(first == null ? '' : first).trim() || 'eng';
    const b = String(second == null ? '' : second).trim();
    return b && b !== a ? `${a}+${b}` : a;
}

// The spec from the page's selects, or 'eng' when they're absent.
function selectedLanguage(doc) {
    const first = doc.getElementById('ocrLanguage');
    const second = doc.getElementById('ocrLanguage2');
    return joinLanguages(first ? first.value : '', second ? second.value : '');
}

if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        joinLanguages,
        selectedLanguage,
    };
}
