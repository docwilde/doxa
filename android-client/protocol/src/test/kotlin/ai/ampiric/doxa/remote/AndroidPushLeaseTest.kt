package ai.ampiric.doxa.remote

import org.junit.Assert.*
import org.junit.Test

class AndroidPushLeaseTest {
    @Test fun shortLeaseRenewsBeforeExpiryAndExpiresOnForegroundReturn() {
        val lease = AndroidPushLease()
        assertFalse(lease.current(100))
        assertEquals(0L, lease.renewIn(100))
        assertTrue(lease.accept(100, 2))
        assertEquals(1_800L, lease.renewIn(100))
        assertTrue(lease.current(1_900))
        assertEquals(0L, lease.renewIn(1_900))
        assertFalse(lease.current(2_100))
        assertEquals(0L, lease.renewIn(2_100))
    }
    @Test fun failedRenewalCannotKeepAnOldRegisteredProof() {
        val lease = AndroidPushLease()
        assertTrue(lease.accept(100, 2))
        lease.clear()
        assertFalse(lease.current(150))
        assertEquals(0L, lease.renewIn(150))
        assertFalse(lease.accept(150, 0))
        assertFalse(lease.accept(150, 86_401))
        assertFalse(lease.accept(Long.MAX_VALUE, 1))
        assertTrue(lease.accept(200, 1))
        assertTrue(lease.current(200))
    }
}
