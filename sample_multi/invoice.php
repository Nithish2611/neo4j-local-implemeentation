<?php
function calculate_tax($a) { return $a * 0.2; }
class Invoice { function total($a) { return calculate_tax($a) + $this->fee(); } function fee() { return 1; } }
