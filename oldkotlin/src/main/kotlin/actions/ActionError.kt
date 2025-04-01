package actions

/**
 * An avoidable error that occurs when an action is picked that is not suitable.  This is modeled as an exception
 * because I know it'll be easy to fold in, but maybe this should just be a regular object.
 *
 * The impetus for this class is to give us a means of distinguishing between nonsense moves that ML in particular might
 * make, and coding errors
 *
 * Think: "You can't sell Food - you don't have any!"
 */
open class ActionError(message: String) : Exception(message)

object NoError : ActionError("No errors were encountered last turn")