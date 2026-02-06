async function search() {
    const url = document.getElementById("urlInput").value;
    const resultArea = document.getElementById("result");

    resultArea.textContent = "検索中…";

    try {
        const res = await fetch("/api/monitor", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({ url })
        });

        const data = await res.json();
        resultArea.textContent = JSON.stringify(data, null, 2);

    } catch (e) {
        resultArea.textContent = "エラーが発生しました";
    }
}
