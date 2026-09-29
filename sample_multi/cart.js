function formatPrice(x) { return "$" + x; }
const showTotal = (a) => { console.log(formatPrice(a)); };
class Cart { add(i) { this.items.push(i); showTotal(1); } }
items.forEach(i => showTotal(i));
