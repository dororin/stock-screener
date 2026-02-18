document.getElementById('screenBtn').addEventListener('click', async () => {
    const btn = document.getElementById('screenBtn');
    const btnText = btn.querySelector('.btn-text');
    const loader = btn.querySelector('.loader');
    const resultsWrapper = document.getElementById('resultsWrapper');
    const emptyState = document.getElementById('emptyState');
    const resultsBody = document.getElementById('resultsBody');

    // UI Feedback: Start Loading
    btn.disabled = true;
    btnText.textContent = '実行中...';
    loader.classList.remove('hidden');

    try {
        const response = await fetch('/api/screen');
        const data = await response.json();

        // Clear previous results
        resultsBody.innerHTML = '';

        if (data.results && data.results.length > 0) {
            data.results.forEach(stock => {
                const tr = document.createElement('tr');
                const changeClass = stock.change >= 0 ? 'positive' : 'negative';
                const changePrefix = stock.change >= 0 ? '+' : '';

                tr.innerHTML = `
                    <td class="stock-code">${stock.code}</td>
                    <td>${stock.name}</td>
                    <td>¥${stock.price.toLocaleString()}</td>
                    <td class="${changeClass}">${changePrefix}${stock.change}%</td>
                    <td>${stock.per}</td>
                    <td>${stock.pbr}</td>
                `;
                resultsBody.appendChild(tr);
            });

            resultsWrapper.classList.remove('hidden');
            emptyState.classList.add('hidden');
        } else {
            resultsBody.innerHTML = '<tr><td colspan="6" style="text-align: center; padding: 2rem;">条件に一致する銘柄が見つかりませんでした。</td></tr>';
            resultsWrapper.classList.remove('hidden');
            emptyState.classList.add('hidden');
        }
    } catch (error) {
        console.error('Error fetching data:', error);
        alert('エラーが発生しました。サーバーとの通信に失敗しました。');
    } finally {
        // UI Feedback: Stop Loading
        btn.disabled = false;
        btnText.textContent = 'スクリーニング実行';
        loader.classList.add('hidden');
    }
});
