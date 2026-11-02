// Ask for confirmation before deleting
const btnDelete = document.querySelectorAll('.btn-delete');

btnDelete.forEach((btn) => {
    btn.addEventListener('click', (e) => {
        if (!confirm('¿Estás seguro de que deseas eliminarlo?')) {
            e.preventDefault();
        }
    });
});
