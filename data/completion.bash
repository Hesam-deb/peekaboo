# bash completion for peekaboo
_peekaboo() {
    local cur=${COMP_WORDS[COMP_CWORD]} words
    case ${COMP_WORDS[1]} in
        use) words="lock login shell system all none" ;;
        set) case ${COMP_WORDS[2]} in
                head-turn) words="lock login shell system all none" ;;
                attention) words="on off" ;;
                level) words="standard high maximum" ;;
                *) words="head-turn attention level" ;;
             esac ;;
        animation) words="on off studio" ;;
        enroll) words="--ir-only --camera" ;;
        test) words="--gesture" ;;
        remove) words="--all" ;;
        cameras) words="--probe" ;;
        history) words="--clear" ;;
        *) [[ $COMP_CWORD -eq 1 ]] && words="gui status enroll test remove use set cameras history animation doctor logs --user --version --help" ;;
    esac
    mapfile -t COMPREPLY < <(compgen -W "$words" -- "$cur")
}
complete -F _peekaboo peekaboo
